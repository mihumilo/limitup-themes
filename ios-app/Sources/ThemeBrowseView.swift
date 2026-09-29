import SwiftUI

/// 复盘主题浏览：官方转录的主题分类 → 展开看成员股（关键词 / 封板时间 / 连板）
struct ThemeBrowseView: View {
    @EnvironmentObject var store: AppStore
    @State private var themes: [ThemeGroup] = []
    @State private var verified = false
    @State private var state: LoadState = .idle
    @State private var expanded: Set<String> = []
    @State private var searchText = ""

    var body: some View {
        NavigationStack {
            Group {
                if case .loading = state {
                    StateView(state: .loading, message: nil, retry: {})
                } else if case .failed(let e) = state {
                    StateView(state: .failed(e), message: nil, retry: { Task { await load() } })
                } else if themes.isEmpty {
                    ContentUnavailableViewCompat(
                        title: "该日无官方复盘主题",
                        systemImage: "doc.text.magnifyingglass",
                        detail: "可能为非交易日，或官方复盘尚未发布"
                    )
                } else {
                    themeList
                }
            }
            .navigationTitle("复盘主题")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { DateMenu() }
            }
            .searchable(text: $searchText, prompt: "搜索主题 / 股票 / 关键词")
            .refreshable { await load(forceDay: true) }
            .task {
                if store.selectedDate.isEmpty, let latest = store.days.last { store.selectedDate = latest }
                await load()
            }
            .onChange(of: store.selectedDate) { _ in Task { await load() } }
        }
    }

    private func load(forceDay: Bool = false) async {
        if forceDay { await store.loadDay(force: true) }
        state = themes.isEmpty ? .loading : .refreshing
        defer { if case .refreshing = state { state = .loaded } }
        if let r = await store.loadThemes(for: store.selectedDate) {
            themes = r.themes
            verified = r.verified
            state = .loaded
        } else {
            themes = []
            state = .loaded
        }
    }

    private var filteredThemes: [ThemeGroup] {
        let q = searchText.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return themes }
        return themes.filter {
            $0.name.localizedCaseInsensitiveContains(q)
                || ($0.stocks ?? []).contains {
                    $0.displayName.localizedCaseInsensitiveContains(q)
                        || ($0.keyword ?? "").localizedCaseInsensitiveContains(q)
                }
        }
    }

    private var themeList: some View {
        ScrollView {
            VStack(spacing: 10) {
                HStack {
                    Text("\(themes.count) 个主题 · \(themes.reduce(0) { $0 + ($1.codes?.count ?? $1.stocks?.count ?? 0) }) 只")
                        .font(.footnote).foregroundStyle(.secondary)
                    Spacer()
                    Text(verified ? "OCR 已校验" : "未校验")
                        .font(.caption2)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(verified ? Color.upRed.opacity(0.12) : Color.boardOrange.opacity(0.14))
                        .foregroundStyle(verified ? .upRed : .boardOrange)
                        .clipShape(Capsule())
                }
                ForEach(filteredThemes) { theme in
                    ThemeCard(
                        theme: theme,
                        expanded: expanded.contains(theme.name),
                        toggle: {
                            withAnimation(.snappy) {
                                if expanded.contains(theme.name) { expanded.remove(theme.name) }
                                else { expanded.insert(theme.name) }
                            }
                        }
                    )
                }
                if filteredThemes.isEmpty {
                    Text("没有匹配的主题").font(.footnote).foregroundStyle(.secondary)
                        .padding(.vertical, 30)
                }
            }
            .padding(.horizontal)
            .padding(.bottom, 20)
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }
}

struct ThemeCard: View {
    let theme: ThemeGroup
    let expanded: Bool
    let toggle: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(action: toggle) {
                HStack {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color.hue(for: theme.name))
                        .frame(width: 4, height: 18)
                    Text(theme.name).font(.body.bold()).foregroundStyle(.primary)
                    Spacer()
                    Text("\(theme.codes?.count ?? theme.stocks?.count ?? 0) 只")
                        .font(.footnote).foregroundStyle(.secondary)
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.plain)
            if expanded {
                ForEach(theme.stocks ?? [ThemeStock]()) { s in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(s.displayName).font(.subheadline.bold())
                        Text(s.code).font(.caption).foregroundStyle(.secondary)
                        if let st = s.streak, st >= 2 {
                            Text("\(st)板").font(.caption2).foregroundStyle(.upRed)
                        }
                        Spacer()
                        if let kw = s.keyword, !kw.isEmpty {
                            Text(kw).font(.caption2).foregroundStyle(.tertiary)
                                .lineLimit(1)
                        }
                        if let t = s.time, !t.isEmpty {
                            Text(String(t.prefix(5))).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 3)
                }
                if (theme.stocks ?? []).isEmpty, let codes = theme.codes {
                    Text(codes.joined(separator: "、"))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(Color.cardBg)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// 主题名 → 稳定色相（同一主题每天同色，与网页看板观感一致）
extension Color {
    static func hue(for name: String) -> Color {
        var h: UInt64 = 0
        for ch in name.unicodeScalars { h = h &* 31 &+ UInt64(ch.value) }
        let hue = Double(h % 3600) / 3600.0
        return Color(hue: hue, saturation: 0.55, brightness: 0.55)
    }
}
