import SwiftUI

/// 涨停列表：按连板高度分组 + 高度筛选 + 搜索 + 下拉刷新
struct LimitUpListView: View {
    @EnvironmentObject var store: AppStore
    @State private var searchText = ""
    @State private var heightFilter: HeightFilter = .all

    var body: some View {
        NavigationStack {
            Group {
                if case .loading = store.dataState {
                    StateView(state: .loading, message: nil, retry: {})
                } else if case .failed(let e) = store.dataState {
                    StateView(state: .failed(e), message: nil, retry: {
                        Task { await store.loadDay(force: false) }
                    })
                } else if store.dayData?.stocks.isEmpty ?? true {
                    ContentUnavailableViewCompat(
                        title: "该日无涨停数据",
                        systemImage: "tray",
                        detail: store.dataMessage.isEmpty ? "可能为非交易日" : store.dataMessage
                    )
                } else {
                    stockList
                }
            }
            .navigationTitle("涨停 · " + (DateMenu.label(store.selectedDate)))
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { DateMenu() }
            }
            .searchable(text: $searchText, prompt: "搜索代码 / 名称 / 涨停原因")
            .refreshable { await store.loadDay(force: true) }
            .task {
                if store.selectedDate.isEmpty, let latest = store.days.last { store.selectedDate = latest }
                if store.dayData == nil { await store.loadDay(force: false) }
            }
        }
    }

    // MARK: 筛选

    private enum HeightFilter: String, CaseIterable, Identifiable {
        case all = "全部"
        case high3 = "3板+"
        case high5 = "5板+"
        case first = "首板"
        case broken = "断板"
        var id: String { rawValue }
        func match(_ s: Stock) -> Bool {
            switch self {
            case .all: return true
            case .high3: return s.height >= 3
            case .high5: return s.height >= 5
            case .first: return s.height == 1
            case .broken: return s.isBroken
            }
        }
    }

    private var filtered: [Stock] {
        var list = store.dayData?.stocks ?? []
        if heightFilter != .all { list = list.filter { heightFilter.match($0) } }
        let q = searchText.trimmingCharacters(in: .whitespaces)
        if !q.isEmpty {
            list = list.filter {
                $0.c.contains(q) || $0.name.localizedCaseInsensitiveContains(q)
                    || ($0.lur ?? "").localizedCaseInsensitiveContains(q)
            }
        }
        return list.sorted {
            if $0.height != $1.height { return $0.height > $1.height }
            return ($0.fbt ?? "") < ($1.fbt ?? "")
        }
    }

    // MARK: 列表（按高度分组）

    private var stockList: some View {
        let groups = Dictionary(grouping: filtered, by: \.height)
        let keys = groups.keys.sorted(by: >)
        return ScrollView {
            VStack(spacing: 12) {
                filterChips
                summaryBar
                ForEach(keys, id: \.self) { h in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(groupTitle(h, count: groups[h]?.count ?? 0))
                            .font(.subheadline.bold())
                            .foregroundStyle(h >= 2 ? .upRed : .secondary)
                        ForEach(groups[h] ?? []) { s in
                            StockRow(stock: s)
                        }
                    }
                }
                if filtered.isEmpty {
                    Text("没有匹配的个股").font(.footnote).foregroundStyle(.secondary)
                        .padding(.vertical, 30)
                }
            }
            .padding(.horizontal)
            .padding(.bottom, 20)
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }

    private func groupTitle(_ h: Int, count: Int) -> String {
        h >= 2 ? "\(h) 连板 · \(count) 只" : "首板 · \(count) 只"
    }

    private var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(HeightFilter.allCases) { f in
                    Button {
                        withAnimation(.snappy) { heightFilter = f }
                    } label: {
                        Text(f.rawValue)
                            .font(.footnote)
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(heightFilter == f ? Color.upRed : Color.cardBg)
                            .foregroundStyle(heightFilter == f ? Color.white : Color.primary)
                            .clipShape(Capsule())
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }

    private var summaryBar: some View {
        let d = store.dayData
        return HStack(spacing: 16) {
            Label("涨停 \(d?.stocks.count ?? 0)", systemImage: "arrow.up.circle.fill")
                .foregroundStyle(.upRed)
            Label("炸板 \(d?.blowCount ?? 0)", systemImage: "xmark.circle.fill")
                .foregroundStyle(.boardOrange)
            Label("跌停 \(d?.limitDownCount ?? 0)", systemImage: "arrow.down.circle.fill")
                .foregroundStyle(.downGreen)
            Spacer()
        }
        .font(.footnote.bold())
        .padding(10)
        .background(Color.cardBg)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// 个股行：名称/代码 + 高度徽标 + 封板时间 + 原因（换手率）
struct StockRow: View {
    let stock: Stock

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(stock.heightLabel)
                .font(.caption.bold())
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background(badgeColor.opacity(0.14))
                .foregroundStyle(badgeColor)
                .frame(minWidth: 52)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(stock.name).font(.body.bold())
                    Text(stock.c).font(.caption).foregroundStyle(.secondary)
                    if stock.isBroken {
                        Text("断").font(.caption2).foregroundStyle(.boardOrange)
                    }
                }
                if let lur = stock.lur, !lur.isEmpty {
                    Text(lur).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                if let fbt = stock.fbt, !fbt.isEmpty {
                    Text(String(fbt.prefix(5))).font(.caption).foregroundStyle(.secondary)
                }
                if let tr = stock.tr {
                    Text(String(format: "换手 %.1f%%", tr))
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 8).padding(.horizontal, 12)
        .background(Color.cardBg)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var badgeColor: Color {
        stock.height >= 5 ? .upRed : (stock.height >= 3 ? .boardOrange : .secondary)
    }
}

/// iOS 16 兼容的 ContentUnavailableView 替代（17+ 有原生）
struct ContentUnavailableViewCompat: View {
    let title: String
    let systemImage: String
    var detail: String = ""

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: systemImage).font(.system(size: 36)).foregroundStyle(.secondary)
            Text(title).font(.headline)
            if !detail.isEmpty {
                Text(detail).font(.footnote).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}
