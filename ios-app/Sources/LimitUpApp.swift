import SwiftUI

@main
struct LimitUpApp: App {
    @StateObject private var store = AppStore()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .tint(.red)
        }
    }
}

/// 根视图：未登录 → 登录页；已登录 → 主 Tab
struct RootView: View {
    @EnvironmentObject var store: AppStore

    var body: some View {
        Group {
            if store.signedIn {
                MainTabView()
            } else {
                LoginView()
            }
        }
        .task { await store.restoreSession() }
    }
}

/// 主导航：iOS 规范 Tab 结构（概览 / 涨停列表 / 复盘主题 / 设置）
struct MainTabView: View {
    var body: some View {
        TabView {
            OverviewView()
                .tabItem { Label("概览", systemImage: "chart.xyaxis.line") }
            LimitUpListView()
                .tabItem { Label("涨停", systemImage: "list.bullet.rectangle") }
            ThemeBrowseView()
                .tabItem { Label("复盘", systemImage: "doc.text.magnifyingglass") }
            SettingsView()
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
    }
}
