// Prodder.app — native launcher: runs the prodder server, shows the carrot in
// the Dock and the menu bar (with a live agent/stalled count), opens the
// dashboard, and cleanly stops everything on quit. No third-party deps.
//
// Build:  swiftc macapp/Prodder.swift -O -o Prodder.app/Contents/MacOS/prodder -framework Cocoa
import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: Process?
    var statusItem: NSStatusItem!
    var baseURL = "http://127.0.0.1:8737"
    var timer: Timer?
    var opened = false
    var autoOn = true
    let statusLine = NSMenuItem(title: "starting…", action: nil, keyEquivalent: "")
    let autoItem = NSMenuItem(title: "Auto-prod", action: #selector(toggleAuto),
                              keyEquivalent: "")

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)          // show in the Dock
        buildMenu()
        if serverAlreadyUp() {
            serverReady(baseURL)                      // attach to a running instance
        } else {
            startServer()
        }
    }

    // MARK: server lifecycle

    func repoDir() -> String {
        Bundle.main.bundleURL.deletingLastPathComponent().path
    }

    func pickPython() -> String {
        for c in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/usr/bin/python3"] where FileManager.default.isExecutableFile(atPath: c) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: c)
            p.arguments = ["-c", "import tomllib"]
            p.standardError = Pipe(); p.standardOutput = Pipe()
            try? p.run(); p.waitUntilExit()
            if p.terminationStatus == 0 { return c }
        }
        return "/usr/bin/python3"
    }

    func serverAlreadyUp() -> Bool {
        guard let u = URL(string: baseURL + "/api/state") else { return false }
        var req = URLRequest(url: u); req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0); var up = false
        URLSession.shared.dataTask(with: req) { d, _, _ in
            up = (d != nil); sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return up
    }

    func startServer() {
        let proc = Process()
        proc.currentDirectoryURL = URL(fileURLWithPath: repoDir())
        proc.executableURL = URL(fileURLWithPath: pickPython())
        proc.arguments = ["prodtop.py", "--no-browser"]
        let pipe = Pipe()
        proc.standardError = pipe
        proc.standardOutput = pipe
        pipe.fileHandleForReading.readabilityHandler = { fh in
            let s = String(data: fh.availableData, encoding: .utf8) ?? ""
            if let r = s.range(of: "http://127.0.0.1:[0-9]+",
                               options: .regularExpression) {
                let url = String(s[r])
                DispatchQueue.main.async { self.serverReady(url) }
            }
        }
        do {
            try proc.run()
            server = proc
        } catch {
            alert("Couldn't start the prodder server.\n\(error.localizedDescription)")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            if self.timer == nil { self.serverReady(self.baseURL) }   // fallback
        }
    }

    func serverReady(_ url: String) {
        baseURL = url
        if !opened, let u = URL(string: url) {
            opened = true
            NSWorkspace.shared.open(u)
        }
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { _ in
                self.poll()
            }
            poll()
        }
    }

    // MARK: menu bar

    func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🥕"
        let menu = NSMenu()
        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Dashboard", action: #selector(openDash),
                              keyEquivalent: "o")
        menu.addItem(open)
        autoItem.state = .on
        menu.addItem(autoItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Prodder", action: #selector(quit),
                                keyEquivalent: "q"))
        for it in menu.items { it.target = self }
        statusItem.menu = menu
    }

    @objc func openDash() {
        if let u = URL(string: baseURL) { NSWorkspace.shared.open(u) }
    }

    @objc func toggleAuto() {
        autoOn.toggle()
        autoItem.state = autoOn ? .on : .off
        post(["action": "autoprod", "value": autoOn])
    }

    @objc func quit() { NSApp.terminate(nil) }

    // MARK: polling / control

    func poll() {
        guard let u = URL(string: baseURL + "/api/state") else { return }
        URLSession.shared.dataTask(with: u) { data, _, _ in
            guard let d = data,
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
            else { return }
            let n = j["agent_count"] as? Int ?? 0
            let s = j["stalled_count"] as? Int ?? 0
            let auto = j["auto_prod"] as? Bool ?? true
            DispatchQueue.main.async {
                self.statusLine.title = "\(n) agents · \(s) stalled"
                self.statusItem.button?.title = s > 0 ? "🥕 \(s)" : "🥕"
                self.autoOn = auto
                self.autoItem.state = auto ? .on : .off
            }
        }.resume()
    }

    func post(_ obj: [String: Any]) {
        guard let u = URL(string: baseURL + "/api/action"),
              let body = try? JSONSerialization.data(withJSONObject: obj) else { return }
        var r = URLRequest(url: u)
        r.httpMethod = "POST"
        r.setValue("1", forHTTPHeaderField: "X-Prodder")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = body
        URLSession.shared.dataTask(with: r).resume()
    }

    func alert(_ msg: String) {
        let a = NSAlert(); a.messageText = "Prodder"; a.informativeText = msg
        a.runModal()
    }

    // Only stop the server if WE started it — leave an attached instance alone.
    func applicationWillTerminate(_ note: Notification) {
        if server != nil {
            post(["action": "quit"])
            server?.terminate()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool {
        false
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
