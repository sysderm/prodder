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

    func pickPython() -> String? {
        for c in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/usr/bin/python3"] where FileManager.default.isExecutableFile(atPath: c) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: c)
            p.arguments = ["-c", "import tomllib"]
            p.standardError = Pipe(); p.standardOutput = Pipe()
            try? p.run(); p.waitUntilExit()
            if p.terminationStatus == 0 { return c }
        }
        return nil    // no Python 3.11+ with tomllib — caller must surface this,
                      // NOT silently launch a stock 3.9 that dies on `import tomllib`
    }

    func logFileURL() -> URL {
        let logs = FileManager.default.urls(for: .libraryDirectory,
                                            in: .userDomainMask)[0]
            .appendingPathComponent("Logs")
        try? FileManager.default.createDirectory(at: logs,
                                                 withIntermediateDirectories: true)
        return logs.appendingPathComponent("prodder.log")
    }

    func openLog() -> FileHandle? {
        let url = logFileURL()
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        let h = try? FileHandle(forWritingTo: url)
        _ = try? h?.seekToEnd()          // append, don't truncate
        return h
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
        guard let python = pickPython() else {
            alert("Prodder needs Python 3.11+ (with the stdlib \"tomllib\").\n" +
                  "Install a newer Python — e.g.  brew install python  — then " +
                  "reopen Prodder.")
            return
        }
        let proc = Process()
        proc.currentDirectoryURL = URL(fileURLWithPath: repoDir())
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = ["prodtop.py", "--no-browser"]
        let log = openLog()          // tee the engine's output to ~/Library/Logs/prodder.log
        let pipe = Pipe()
        proc.standardError = pipe
        proc.standardOutput = pipe
        pipe.fileHandleForReading.readabilityHandler = { fh in
            let data = fh.availableData
            log?.write(data)
            let s = String(data: data, encoding: .utf8) ?? ""
            if let r = s.range(of: "http://127.0.0.1:[0-9]+",
                               options: .regularExpression) {
                let url = String(s[r])
                DispatchQueue.main.async { self.serverReady(url) }
            }
        }
        // If the engine exits before we ever saw a URL, it failed to start —
        // surface the tail of the log instead of opening a dead dashboard.
        proc.terminationHandler = { p in
            try? log?.close()
            if !self.opened && p.terminationStatus != 0 {
                let tail = (try? String(contentsOf: self.logFileURL(),
                                        encoding: .utf8))?.suffix(500) ?? ""
                DispatchQueue.main.async {
                    self.alert("The prodder server stopped before it was ready.\n" +
                               "See ~/Library/Logs/prodder.log\n\n\(tail)")
                }
            }
        }
        do {
            try proc.run()
            server = proc
        } catch {
            alert("Couldn't start the prodder server.\n\(error.localizedDescription)")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            if self.timer == nil && self.server?.isRunning == true {
                self.serverReady(self.baseURL)   // fallback: assume default port
            }
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

    func apiKey() -> String {
        // The engine writes its per-session token next to prodtop.py (0600).
        let p = repoDir() + "/prodder-token"
        return (try? String(contentsOfFile: p, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    func post(_ obj: [String: Any]) {
        guard let u = URL(string: baseURL + "/api/action"),
              let body = try? JSONSerialization.data(withJSONObject: obj) else { return }
        var r = URLRequest(url: u)
        r.httpMethod = "POST"
        r.setValue("1", forHTTPHeaderField: "X-Prodder")
        r.setValue(apiKey(), forHTTPHeaderField: "X-Prodder-Key")
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
