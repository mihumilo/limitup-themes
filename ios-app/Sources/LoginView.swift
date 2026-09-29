import SwiftUI

/// 登录页：服务器地址 + 访问密码（Worker /api/login）
struct LoginView: View {
    @EnvironmentObject var store: AppStore
    @AppStorage("lp.lastServer") private var lastServer = ""
    @State private var server = ""
    @State private var password = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 22) {
            Spacer()
            VStack(spacing: 6) {
                Text("涨停复盘")
                    .font(.largeTitle.bold())
                    .foregroundStyle(.upRed)
                Text("iPhone 原生客户端 · Cloudflare Worker")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            VStack(spacing: 12) {
                TextField("服务器地址（https://xxx.workers.dev）", text: $server)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                    .font(.subheadline)
                SecureField("访问密码（服务端未配密码可留空）", text: $password)
                    .textFieldStyle(.roundedBorder)
                    .font(.subheadline)
                    .onSubmit { Task { await go() } }

                Button {
                    Task { await go() }
                } label: {
                    if busy {
                        HStack { ProgressView(); Text("连接中…") }
                            .frame(maxWidth: .infinity)
                    } else {
                        Text("进入看板")
                            .frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(busy || server.trimmingCharacters(in: .whitespaces).isEmpty)

                if let error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                }
                if store.sessionExpired {
                    Text("登录已过期（有效期 7 天），请重新登录")
                        .font(.footnote)
                        .foregroundStyle(.boardOrange)
                }
            }
            .padding(.horizontal, 28)

            Spacer()
            Spacer()

            Text("数据由您的 Cloudflare Worker 提供，与网页看板同源同权限。")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .padding(.bottom, 18)
        }
        .onAppear {
            if server.isEmpty {
                server = store.serverURL.isEmpty ? lastServer : store.serverURL
            }
        }
    }

    private func go() async {
        busy = true
        error = nil
        if let err = await store.login(server: server, password: password) {
            error = err
        } else {
            lastServer = store.serverURL
        }
        busy = false
    }
}
