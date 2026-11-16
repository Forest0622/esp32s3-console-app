// 原生窗口外壳：NSWindow + WKWebView，不使用外部浏览器进程。
// 后台仍由打包进去的 Python 服务提供数据，窗口本身是 App 自己的。

import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var loaded = false

    private let supportDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        return base.appendingPathComponent("ESP32-S3-Console-App", isDirectory: true)
    }()

    private var portFileURL: URL { supportDir.appendingPathComponent("port.txt") }
    private var dataHomeURL: URL { supportDir.appendingPathComponent("data", isDirectory: true) }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        makeWindow()
        launchServer()
        waitForPort(attempt: 0)
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.terminate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    // ---------------------------------------------------------------- UI

    private func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 ESP32-S3 检修台",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = mainMenu
    }

    private func makeWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1180, height: 760)
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "ESP32-S3 检修台"
        window.delegate = self
        window.minSize = NSSize(width: 940, height: 620)
        window.setFrameAutosaveName("ConsoleAppWindow")
        window.center()

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        // 前端点「选择…」时，由 App 弹出系统访达面板，而不是让用户手打路径。
        config.userContentController.add(self, name: "openPanel")
        // 备份旁边的小图标：悬停才显示「在访达中打开」，点它打开备份文件夹
        config.userContentController.add(self, name: "openBackups")
        webView = WKWebView(frame: frame, configuration: config)
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // ---------------------------------------------------------------- 访达选文件

    func userContentController(_ userContentController: WKUserContentController,
                              didReceive message: WKScriptMessage) {
        if message.name == "openBackups" {
            let dir = dataHomeURL.appendingPathComponent("backups", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir,
                                                     withIntermediateDirectories: true)
            NSWorkspace.shared.open(dir)
            return
        }
        guard message.name == "openPanel" else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        panel.message = "选择 ESP-IDF 工程文件夹，或选择 zip 压缩包"
        panel.prompt = "选择工程"
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
        let picked: String
        if panel.runModal() == .OK, let url = panel.url {
            picked = url.path
        } else {
            picked = ""
        }
        let literal = AppDelegate.jsLiteral(picked)
        webView.evaluateJavaScript("window.__pickedPath(\(literal));", completionHandler: nil)
    }

    static func jsLiteral(_ value: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [value])) ?? Data("[]".utf8)
        let text = String(data: data, encoding: .utf8) ?? "[]"
        return String(text.dropFirst().dropLast())
    }

    // ---------------------------------------------------------------- 服务

    private func launchServer() {
        guard let resources = Bundle.main.resourceURL else { return }
        let binary = resources.appendingPathComponent("backend/console_app")
        guard FileManager.default.isExecutableFile(atPath: binary.path) else {
            showFatal("应用内容不完整：缺少 backend/console_app")
            return
        }

        try? FileManager.default.createDirectory(at: dataHomeURL,
                                                 withIntermediateDirectories: true)
        try? FileManager.default.removeItem(at: portFileURL)

        let proc = Process()
        proc.executableURL = binary
        var env = ProcessInfo.processInfo.environment
        env["CONSOLE_APP_HOME"] = dataHomeURL.path
        env["CONSOLE_APP_FRONTEND"] = resources.appendingPathComponent("frontend-ui").path
        env["CONSOLE_APP_COMPONENTS"] = resources.appendingPathComponent("esp32_components").path
        let seed = resources.appendingPathComponent("backups")
        if FileManager.default.fileExists(atPath: seed.path) {
            env["CONSOLE_APP_SEED_BACKUPS"] = seed.path
        }
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
        proc.environment = env

        do {
            try proc.run()
            server = proc
        } catch {
            showFatal("无法启动后台服务：\(error.localizedDescription)")
        }
    }

    /// 后台会先把实际端口写进 port.txt；读不到就重试，最多约 12 秒。
    private func waitForPort(attempt: Int) {
        if loaded { return }
        if let text = try? String(contentsOf: portFileURL, encoding: .utf8),
           let port = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)),
           port > 0 {
            var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!)
            req.timeoutInterval = 10
            webView.load(req)
            loaded = true
            return
        }
        if attempt > 40 {
            showFatal("后台服务启动超时。请在「控制台」应用中查看日志。")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.waitForPort(attempt: attempt + 1)
        }
    }

    private func showFatal(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "启动失败"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.runModal()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
