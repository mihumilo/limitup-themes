import Foundation

// MARK: - 错误类型

enum APIError: LocalizedError {
    case badURL
    case http(Int)
    case unauthorized          // 401 → 回登录页
    case business(String)      // Worker 业务错误（code != 0）
    case empty                 // EMPTY_*：非交易日 / 无数据
    case network(String)

    var errorDescription: String? {
        switch self {
        case .badURL: return "服务器地址无效，请到设置页检查"
        case .http(let s): return "服务返回异常（HTTP \(s)）"
        case .unauthorized: return "登录已过期，请重新登录"
        case .business(let m): return m
        case .empty: return nil   // 特殊：非交易日不算错误，UI 层单独展示
        case .network(let m): return "网络错误：\(m)"
        }
    }
}

// MARK: - 数据模型（字段名与 Worker 返回一一对应）

/// 涨停池个股（/api/data data.lu[]）
struct Stock: Codable, Identifiable, Hashable {
    let c: String            // 代码
    let n: String?           // 名称
    let lbc: Int?            // 连板高度（连板天梯口径）
    let fbt: String?         // 首次封板时间 HH:MM:SS
    let lur: String?         // 涨停原因 / 所属主题
    let hd: String?          // high_days 原文（如 4天3板）
    let gap: String?         // 断板备注（N天M板，N>M 时）
    let tr: Double?          // 换手率
    let chg: Double?         // 涨跌幅

    var id: String { c }
    var name: String { n ?? c }
    var height: Int { lbc ?? 1 }
    var isBroken: Bool { !(gap ?? "").isEmpty }
    var heightLabel: String {
        if height >= 2 { return "\(height)连板" }
        return isBroken ? (gap ?? "首板") : "首板"
    }
}

/// 炸板（/api/data data.bl[]）
struct BlowStock: Codable, Identifiable, Hashable {
    let c: String
    let n: String?
    let zbc: Int?      // 炸板次数
    let fbt: String?   // 首次封板时间
    let hy: String?    // 行业
    var id: String { c }
    var name: String { n ?? c }
}

/// 跌停（/api/data data.ld[]）
struct LimitDownStock: Codable, Identifiable, Hashable {
    let c: String
    let n: String?
    let hy: String?
    var id: String { c }
    var name: String { n ?? c }
}

/// 官方复盘主题（GitHub 转录 JSON，Worker 原样透传）
struct ThemeGroup: Codable, Identifiable, Hashable {
    let name: String
    let count: Int?
    let codes: [String]?
    let stocks: [ThemeStock]?
    var id: String { name }
}

struct ThemeStock: Codable, Identifiable, Hashable {
    let code: String
    let name: String?
    let keyword: String?
    let time: String?
    let streak: Int?
    var id: String { code }
    var displayName: String { name ?? code }
}

/// 主题包（/api/data data.th 或 /api/themes）
struct ThemePack: Codable {
    let date: String?
    let verified: Bool?
    let total: Int?
    let themes: [ThemeGroup]?
}

/// 单日完整数据（/api/data data）
struct DayData: Codable {
    let lu: [Stock]?
    let bl: [BlowStock]?
    let ld: [LimitDownStock]?
    let th: ThemePack?

    var stocks: [Stock] { lu ?? [] }
    var limitDownCount: Int { ld?.count ?? 0 }
    var blowCount: Int { bl?.count ?? 0 }
    /// 晋级率：昨日连板(≥2)今日仍涨停的比例（prev.data.lu 提供）
    func promoteRate(prev: DayData?) -> Int? {
        guard let prevLu = prev?.lu, !prevLu.isEmpty else { return nil }
        let yesterday = Set(prevLu.filter { ($0.lbc ?? 1) >= 2 }.map(\.c))
        guard !yesterday.isEmpty else { return nil }
        let today = Set(stocks.map(\.c))
        let hit = yesterday.intersection(today).count
        return Int((Double(hit) / Double(yesterday.count) * 100).rounded())
    }
}

/// /api/data 整体响应
struct DataResponse: Codable {
    let code: Int?
    let source: String?
    let message: String?
    let data: DayData?
    let prev: PrevDay?
    struct PrevDay: Codable {
        let date: String?
        let code: Int?
        let data: DayData?
    }
}

/// 情绪周期走势单日（/api/board-track items[]）
struct TrackItem: Codable, Identifiable, Hashable {
    let d: String                    // YYYYMMDD
    let v: Bool?                     // verified
    let t: [TrackTheme]?             // 主题 [{n,c,m}]
    let top: TrackTop?               // 当日最高板
    let upN: Int?                    // 涨停家数
    let lbN: Int?                    // 连板家数
    let downN: Int?                  // 跌停家数
    let breakN: Int?                 // 炸板家数
    let adv: Int?                    // 晋级率 0~100

    var id: String { d }
    var upCount: Int { upN ?? 0 }
    var lbCount: Int { lbN ?? 0 }
    var downCount: Int { downN ?? 0 }
    var maxM: Int { top?.m ?? (t?.compactMap(\.m).max() ?? 0) }
}

struct TrackTheme: Codable, Hashable {
    let n: String    // 主题名
    let c: Int?      // 成员数
    let m: Int?      // 该主题最高板
}

struct TrackTop: Codable, Hashable {
    let m: Int?      // 高度
    let n: String?   // 名称
    let c: String?   // 代码
    let th: String?  // 概念
}

struct TrackResponse: Codable {
    let code: Int?
    let days: Int?
    let items: [TrackItem]?
}

/// 交易日索引（/api/days）
struct DaysResponse: Codable {
    let code: Int?
    let trading: [String]?
    let empty: [String]?
}

/// 主题复盘响应（/api/themes）
struct ThemesResponse: Codable {
    let code: Int?
    let date: String?
    let verified: Bool?
    let total: Int?
    let message: String?
    let themes: [ThemeGroup]?
}
