import Foundation

/// 全局状态：服务器配置 / 登录态 / 各页数据
@MainActor
final class AppStore: ObservableObject {
    // MARK: - 持久化键
    private static let kServer = "lp.server"
    private static let kCookie = "lp.cookie"
    private static let kFree = "lp.free"

    @Published var signedIn = false
    @Published var serverURL: String {
        didSet { UserDefaults.standard.set(serverURL, forKey: Self.kServer) }
    }
    /// lp_auth cookie 值（Worker 登录有效期 7 天，过期后 401 回登录页）
    @Published var authCookie: String {
        didSet { UserDefaults.standard.set(authCookie, forKey: Self.kCookie) }
    }
    /// Worker 未配 ADMIN_PASSWORD → 无需登录
    @Published var passwordFree: Bool {
        didSet { UserDefaults.standard.set(passwordFree, forKey: Self.kFree) }
    }
    /// 最近一次 401（RootView 据此回登录页并提示）
    @Published var sessionExpired = false

    // MARK: - 共享数据
    @Published var days: [String] = []                 // 交易日（升序）
    @Published var selectedDate: String = ""           // YYYYMMDD
    @Published var dayData: DayData?
    @Published var prevData: DayData?
    @Published var dataState: LoadState = .idle
    @Published var dataMessage: String = ""
    @Published var trackItems: [TrackItem] = []        // 情绪走势（升序）
    @Published var trackState: LoadState = .idle

    init() {
        serverURL = UserDefaults.standard.string(forKey: Self.kServer) ?? ""
        authCookie = UserDefaults.standard.string(forKey: Self.kCookie) ?? ""
        passwordFree = UserDefaults.standard.bool(forKey: Self.kFree)
        signedIn = !serverURL.isEmpty && (passwordFree || !authCookie.isEmpty)
    }

    func restoreSession() async {
        guard signedIn else { return }
        await refreshDays()
        // 已有缓存日期则补拉当天数据；否则选最新交易日
        if selectedDate.isEmpty {
            selectedDate = days.last ?? Self.beijingToday()
        }
        await loadDay(force: false)
        await loadTrack()
    }

    func signOut() {
        authCookie = ""
        passwordFree = false
        signedIn = false
        dayData = nil
        prevData = nil
        trackItems = []
        days = []
        selectedDate = ""
    }

    /// 登录：POST /api/login {password}。未配密码的 Worker 返回「无需登录」也算成功。
    func login(server: String, password: String) async -> String? {
        var trimmed = server.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.hasPrefix("http") { trimmed = "https://" + trimmed }
        while trimmed.hasSuffix("/") { trimmed.removeLast() }
        guard let url = URL(string: trimmed + "/api/login") else { return "服务器地址无效" }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 15
        req.httpBody = try? JSONEncoder().encode(["password": password])

        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse else { return "服务无响应" }
            guard (200..<300).contains(http.statusCode) else { return "服务返回 HTTP \(http.statusCode)" }
            struct LoginBody: Decodable { let code: Int?; let message: String? }
            let body = try? JSONDecoder().decode(LoginBody.self, from: data)
            if let code = body?.code, code == 0 {
                // 取 Set-Cookie 里的 lp_auth 值（HTTPCookie.cookies 返回非可选数组）
                let cookies = HTTPCookie.cookies(
                    withResponseHeaderFields: http.allHeaderFields as? [String: String] ?? [:],
                    for: url
                )
                if let auth = cookies.first(where: { $0.name == "lp_auth" }) {
                    authCookie = auth.value
                    passwordFree = false
                } else {
                    passwordFree = true   // 没发 cookie 但登录成功（未配密码兜底）
                }
            } else if let msg = body?.message, msg.contains("无需登录") {
                passwordFree = true       // Worker 未配密码 → 免登录
            } else {
                return body?.message ?? "密码错误"
            }
            serverURL = trimmed
            signedIn = true
            sessionExpired = false
            await refreshDays()
            if selectedDate.isEmpty { selectedDate = days.last ?? Self.beijingToday() }
            async let d: Void = loadDay(force: false)
            async let t: Void = loadTrack()
            _ = await (d, t)
            return nil
        } catch {
            return "网络错误：\(error.localizedDescription)"
        }
    }

    func refreshDays() async {
        guard let resp: DaysResponse = try? await get("/api/days") else { return }
        days = resp.trading ?? []
    }

    /// 拉取当日 + 昨日数据（refresh=1 绕过服务端短缓存）
    func loadDay(force: Bool) async {
        dataState = dataData == nil ? .loading : .refreshing
        defer { if case .refreshing = dataState { dataState = .loaded } }
        var path = "/api/data?date=\(selectedDate)&prev=1"
        if force { path += "&refresh=1" }
        do {
            let resp = try await get(path) as DataResponse
            if resp.code == 0 {
                dayData = resp.data
                prevData = resp.prev?.data
                dataMessage = Self.sourceHint(resp.source)
                dataState = .loaded
            } else if let msg = resp.message {
                dataState = .failed(msg)
            } else {
                dataState = .failed("未知错误")
            }
        } catch let err as APIError {
            handle(err, for: .failed(err.localizedDescription))
        } catch {
            dataState = .failed("网络错误：\(error.localizedDescription)")
        }
    }

    func loadTrack() async {
        trackState = trackItems.isEmpty ? .loading : .refreshing
        defer { if case .refreshing = trackState { trackState = .loaded } }
        do {
            let resp = try await get("/api/board-track?days=90") as TrackResponse
            trackItems = (resp.items ?? []).sorted { $0.d < $1.d }
            trackState = .loaded
        } catch let err as APIError {
            handle(err, for: .failed(err.localizedDescription))
        } catch {
            trackState = .failed("网络错误：\(error.localizedDescription)")
        }
    }

    /// 主题复盘：优先用 dayData.th，缺失时回退 /api/themes
    func loadThemes(for date: String) async -> (themes: [ThemeGroup], verified: Bool)? {
        if date == selectedDate, let th = dayData?.th, let list = th.themes, !list.isEmpty {
            return (list, th.verified ?? false)
        }
        guard let resp = try? await get("/api/themes?date=\(date)") as ThemesResponse else { return nil }
        guard let list = resp.themes, !list.isEmpty else { return nil }
        return (list, resp.verified ?? false)
    }

    // MARK: - 底层请求

    private func get<T: Decodable>(_ path: String) async throws -> T {
        guard let url = URL(string: serverURL + path) else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.timeoutInterval = 20
        if !authCookie.isEmpty, !passwordFree {
            req.setValue("lp_auth=\(authCookie)", forHTTPHeaderField: "Cookie")
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError.network("无响应") }
        switch http.statusCode {
        case 200..<300: break
        case 401: throw APIError.unauthorized
        default: throw APIError.http(http.statusCode)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func handle(_ err: APIError, for state: LoadState) {
        switch err {
        case .unauthorized:
            sessionExpired = true
            signOut()
        case .empty:
            dataState = .loaded
            dataMessage = Self.sourceHint("EMPTY")
            if case .failed = state { dataState = .loaded }
        default:
            if case .failed(let m) = state { dataState = .failed(m) }
        }
    }

    // MARK: - 工具

    static func beijingToday() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyyMMdd"
        f.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        return f.string(from: Date())
    }

    private var dataData: DayData? { dayData }

    static func sourceHint(_ source: String?) -> String {
        switch source {
        case "EMPTY_HOLIDAY", "EMPTY", "EMPTY_CACHED", "EMPTY_TODAY":
            return "该日期无数据（非交易日或当日无涨停）"
        case "HIT": return "缓存命中"
        case "REFRESHED": return "已强制刷新"
        case "MISS": return "实时拉取"
        case "PERSISTED": return "历史快照"
        default: return ""
        }
    }
}

// MARK: - 加载状态

enum LoadState: Equatable {
    case idle
    case loading
    case refreshing
    case loaded
    case failed(String)
}
