import SwiftUI

// MARK: - 中国市场配色：红涨绿跌
// 同时扩展 Color 与 ShapeStyle：foregroundStyle(.upRed) 的推断上下文是 ShapeStyle，
// 只写 Color 扩展会让 .upRed 在 ShapeStyle 上下文里找不到（CI 实测的编译错误）。

extension Color {
    static let upRed = Color(red: 0.85, green: 0.17, blue: 0.17)
    static let downGreen = Color(red: 0.08, green: 0.55, blue: 0.35)
    static let boardOrange = Color(red: 0.82, green: 0.60, blue: 0.13)
    static let cardBg = Color(uiColor: .secondarySystemGroupedBackground)
}

extension ShapeStyle where Self == Color {
    static var upRed: Color { .init(red: 0.85, green: 0.17, blue: 0.17) }
    static var downGreen: Color { .init(red: 0.08, green: 0.55, blue: 0.35) }
    static var boardOrange: Color { .init(red: 0.82, green: 0.60, blue: 0.13) }
}

// MARK: - 通用组件

/// 页面级加载 / 错误 / 空态（iOS 规范：居中 + 重试按钮）
struct StateView: View {
    let state: LoadState
    let message: String?
    var emptyText: String = "暂无数据"
    let retry: () -> Void

    var body: some View {
        switch state {
        case .loading:
            VStack(spacing: 12) {
                ProgressView()
                Text("加载中…").font(.footnote).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let err):
            VStack(spacing: 14) {
                Image(systemName: "wifi.exclamationmark")
                    .font(.system(size: 34))
                    .foregroundStyle(.secondary)
                Text(err)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button("重试", action: retry)
                    .buttonStyle(.borderedProminent)
            }
            .padding(32)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        default:
            EmptyView()
        }
    }
}

/// 指标卡（概览页核心指标网格）
struct StatCard: View {
    let title: String
    let value: String
    let color: Color
    var delta: Int? = nil          // 较昨日差值
    var deltaGood: Bool = true     // 差值方向是否算利好

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(value)
                    .font(.title2.bold())
                    .foregroundStyle(color)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                if let d = delta {
                    Text(deltaText(d))
                        .font(.caption2.bold())
                        .foregroundStyle(deltaColor(d))
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color.cardBg)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private func deltaText(_ d: Int) -> String {
        d == 0 ? "—" : (d > 0 ? "▲\(d)" : "▼\(-d)")
    }
    private func deltaColor(_ d: Int) -> Color {
        guard d != 0 else { return .secondary }
        let good = (d > 0) == deltaGood
        return good ? .upRed : .downGreen
    }
}

/// 日期选择菜单（近 N 个交易日）
struct DateMenu: View {
    @EnvironmentObject var store: AppStore

    var body: some View {
        Menu {
            ForEach(store.days.suffix(30).reversed(), id: \.self) { d in
                Button {
                    Task { @MainActor in
                        store.selectedDate = d
                        await store.loadDay(force: false)
                    }
                } label: {
                    if d == store.selectedDate {
                        Label(Self.label(d), systemImage: "checkmark")
                    } else {
                        Text(Self.label(d))
                    }
                }
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "calendar")
                Text(Self.label(store.selectedDate))
            }
            .font(.subheadline)
        }
    }

    static func label(_ key: String) -> String {
        guard key.count == 8 else { return key.isEmpty ? "选择日期" : key }
        let year = String(key.prefix(4))
        let month = String(key.dropFirst(4).prefix(2))
        let day = String(key.suffix(2))
        let m = year + "-" + month + "-" + day
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        if let date = f.date(from: m) {
            let w = Calendar(identifier: .gregorian)
                .dateComponents([.weekday], from: date).weekday ?? 1
            let names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            let y = Int(year) ?? 2026
            if y != currentYear() {
                return m + " " + names[w - 1]
            }
            return month + "-" + day + " " + names[w - 1]
        }
        return m
    }

    private static func currentYear() -> Int {
        let f = DateFormatter()
        f.dateFormat = "yyyy"
        f.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        return Int(f.string(from: Date())) ?? 2026
    }
}
