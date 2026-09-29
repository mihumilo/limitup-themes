import SwiftUI
import Charts

/// 概览页：情绪核心指标 + 90 日情绪周期走势（Swift Charts）
struct OverviewView: View {
    @EnvironmentObject var store: AppStore

    var body: some View {
        NavigationStack {
            Group {
                if store.signedIn {
                    content
                } else {
                    StateView(state: .idle, message: nil, retry: {})
                }
            }
            .navigationTitle("情绪概览")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { DateMenu() }
            }
            .refreshable { await store.loadDay(force: true) }
        }
    }

    private var content: some View {
        ScrollView {
            VStack(spacing: 14) {
                if let msg = hint { hintBar(msg) }
                metricsGrid
                trendCard
                topCard
            }
            .padding(.horizontal)
            .padding(.bottom, 16)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .overlay {
            if case .loading = store.dataState {
                StateView(state: .loading, message: nil, retry: {})
            } else if case .failed(let e) = store.dataState {
                StateView(state: .failed(e), message: nil, retry: { Task { await store.loadDay(force: false) } })
            }
        }
        .task {
            if store.selectedDate.isEmpty, let latest = store.days.last { store.selectedDate = latest }
            if store.dayData == nil { await store.loadDay(force: false) }
            if store.trackItems.isEmpty { await store.loadTrack() }
        }
    }

    private var hint: String? {
        if !store.dataMessage.isEmpty { return store.dataMessage }
        if let d = store.dayData, d.stocks.isEmpty { return "该日期无数据（非交易日或当日无涨停）" }
        return nil
    }

    private func hintBar(_ text: String) -> some View {
        Label(text, systemImage: "info.circle")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.cardBg)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    // MARK: 核心指标（今日 vs 昨日箭头，红涨绿跌按情绪好坏）

    private var metricsGrid: some View {
        let d = store.dayData
        let p = store.prevData
        let upN = d?.stocks.count ?? 0
        let prevUpN = p?.stocks.count
        let ldN = d?.limitDownCount ?? 0
        let prevLdN = p?.limitDownCount
        let blN = d?.blowCount ?? 0
        let prevBlN = p?.blowCount
        let maxM = d?.stocks.map(\.height).max() ?? 0
        let prevMaxM = p?.stocks.map(\.height).max()
        let adv = d?.promoteRate(prev: p)

        return VStack(spacing: 10) {
            HStack(spacing: 10) {
                StatCard(title: "涨停家数", value: "\(upN)", color: .upRed,
                         delta: delta(upN, prevUpN), deltaGood: true)
                StatCard(title: "跌停家数", value: "\(ldN)", color: .downGreen,
                         delta: delta(ldN, prevLdN), deltaGood: false)
            }
            HStack(spacing: 10) {
                StatCard(title: "炸板家数", value: "\(blN)", color: .boardOrange,
                         delta: delta(blN, prevBlN), deltaGood: false)
                StatCard(title: "空间板", value: maxM > 0 ? "\(maxM)板" : "—",
                         color: maxM >= 5 ? .upRed : .primary,
                         delta: delta(maxM, prevMaxM), deltaGood: true)
            }
            HStack(spacing: 10) {
                StatCard(title: "晋级率（昨日连板→今日）", value: adv.map { "\($0)%" } ?? "—",
                         color: (adv ?? 0) >= 35 ? .upRed : .downGreen)
                StatCard(title: "连板家数", value: "\(d?.stocks.filter { $0.height >= 2 }.count ?? 0)",
                         color: .boardOrange)
            }
        }
    }

    private func delta(_ now: Int, _ prev: Int?) -> Int? {
        guard let prev else { return nil }
        return now - prev
    }

    // MARK: 90 日情绪走势

    private var trendCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("情绪周期 · 近 90 日").font(.headline)
                Spacer()
                if store.trackItems.isEmpty, case .loading = store.trackState {
                    ProgressView()
                }
            }
            if store.trackItems.isEmpty {
                Text("暂无走势数据").font(.footnote).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding(.vertical, 24)
            } else {
                Chart {
                    ForEach(store.trackItems) { it in
                        LineMark(x: .value("日期", Self.shortDate(it.d)), y: .value("家数", it.upCount))
                            .foregroundStyle(by: .value("系列", "涨停"))
                        LineMark(x: .value("日期", Self.shortDate(it.d)), y: .value("家数", it.lbCount))
                            .foregroundStyle(by: .value("系列", "连板"))
                        LineMark(x: .value("日期", Self.shortDate(it.d)), y: .value("家数", it.downCount))
                            .foregroundStyle(by: .value("系列", "跌停"))
                    }
                }
                .chartForegroundStyleScale([
                    "涨停": Color.upRed, "连板": Color.boardOrange, "跌停": Color.downGreen
                ])
                .frame(height: 180)

                Divider().padding(.vertical, 4)
                Text("晋级率（昨日连板今日仍涨停 %）").font(.subheadline).foregroundStyle(.secondary)
                Chart {
                    ForEach(store.trackItems) { it in
                        if let a = it.adv {
                            LineMark(x: .value("日期", Self.shortDate(it.d)), y: .value("晋级率", a))
                                .foregroundStyle(Color.blue)
                            AreaMark(x: .value("日期", Self.shortDate(it.d)), y: .value("晋级率", a))
                                .foregroundStyle(Color.blue.opacity(0.08))
                        }
                    }
                }
                .chartYScale(domain: 0...100)
                .frame(height: 110)
            }
        }
        .padding(12)
        .background(Color.cardBg)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    // MARK: 当日最高板

    @ViewBuilder
    private var topCard: some View {
        if let it = store.trackItems.last(where: { $0.d == store.selectedDate }), let top = it.top {
            VStack(alignment: .leading, spacing: 6) {
                Text("当日最高板").font(.headline)
                HStack {
                    Text("\(top.m ?? 0)板").font(.title3.bold()).foregroundStyle(.upRed)
                    Text(top.n ?? "").font(.body)
                    Text(top.th ?? "").font(.footnote).foregroundStyle(.secondary)
                    Spacer()
                    Text(it.v == true ? "已校验" : "未校验")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.cardBg)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
    }

    static func shortDate(_ key: String) -> String {
        key.count == 8 ? String(key.dropFirst(4)) : key
    }
}
