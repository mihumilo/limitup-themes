import SwiftUI

/// 设置页：服务器 / 登录态 / 数据刷新 / 退出
struct SettingsView: View {
    @EnvironmentObject var store: AppStore
    @State private var refreshing = false
    @State private var result: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("服务器") {
                    HStack {
                        Text("地址").foregroundStyle(.secondary)
                        Spacer()
                        Text(store.serverURL)
                            .font(.footnote)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                    HStack {
                        Text("登录态").foregroundStyle(.secondary)
                        Spacer()
                        Text(store.passwordFree ? "免密码（服务端未配置）" : "lp_auth Cookie")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }

                Section("数据") {
                    Button {
                        Task {
                            refreshing = true
                            await store.refreshDays()
                            await store.loadDay(force: true)
                            await store.loadTrack()
                            refreshing = false
                            result = "已强制刷新（绕过服务端缓存）"
                        }
                    } label: {
                        if refreshing {
                            HStack { ProgressView(); Text("刷新中…") }
                        } else {
                            Label("强制刷新全部数据", systemImage: "arrow.clockwise")
                        }
                    }
                    .disabled(refreshing)

                    Picker("查看日期", selection: $store.selectedDate) {
                        ForEach(store.days.suffix(60).reversed(), id: \.self) { d in
                            Text(DateMenu.label(d)).tag(d)
                        }
                    }
                    .onChange(of: store.selectedDate) { _ in
                        Task { await store.loadDay(force: false) }
                    }

                    if let r = result {
                        Text(r).font(.footnote).foregroundStyle(.secondary)
                    }
                    if !store.dataMessage.isEmpty {
                        Text("当前来源：\(store.dataMessage)")
                            .font(.footnote).foregroundStyle(.tertiary)
                    }
                }

                Section {
                    Button(role: .destructive) {
                        store.signOut()
                    } label: {
                        Label("退出登录（清除 Cookie）", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                } footer: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("数据来源：Cloudflare Worker（涨停复盘看板）。")
                        Text("登录有效期 7 天，过期后会自动回到登录页。")
                        Text("构建 \(AppInfo.build)")
                    }
                }
            }
            .navigationTitle("设置")
        }
    }
}

enum AppInfo {
    /// 与 Worker 的 BUILD 无关：iOS 原生壳自己的版本
    static let build = "ios-1.0.0"
}
