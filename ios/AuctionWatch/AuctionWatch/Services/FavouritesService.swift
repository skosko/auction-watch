import Foundation

@Observable
final class FavouritesService {
    private(set) var favourites: Set<String> = []
    private let cacheKey = "favourites"

    // Same proxy the website uses
    private static let proxyBase = "https://auction-watch-proxy.skosko.workers.dev"

    init() {
        loadLocal()
        Task { await fetchRemote() }
    }

    func isFavourite(_ lot: Lot) -> Bool {
        favourites.contains(lot.url)
    }

    func toggle(_ lot: Lot) {
        if favourites.contains(lot.url) {
            favourites.remove(lot.url)
        } else {
            favourites.insert(lot.url)
        }
        saveLocal()
        Task { await pushRemote() }
    }

    // MARK: - Local persistence

    private func loadLocal() {
        if let data = UserDefaults.standard.data(forKey: cacheKey),
           let urls = try? JSONDecoder().decode(Set<String>.self, from: data) {
            favourites = urls
        }
    }

    private func saveLocal() {
        if let data = try? JSONEncoder().encode(favourites) {
            UserDefaults.standard.set(data, forKey: cacheKey)
        }
    }

    // MARK: - Remote sync

    private func fetchRemote() async {
        guard let url = URL(string: "\(Self.proxyBase)/file/favourites") else { return }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return }

            // GitHub Contents API returns { content: base64, sha: ... }
            let ghFile = try JSONDecoder().decode(GitHubFile.self, from: data)
            guard let decoded = Data(base64Encoded: ghFile.content.replacingOccurrences(of: "\n", with: "")) else { return }
            let remote = try JSONDecoder().decode(FavouritesPayload.self, from: decoded)
            favourites = Set(remote.urls)
            saveLocal()
        } catch {
            // File may not exist yet (404) — that's fine
        }
    }

    private func pushRemote() async {
        guard let url = URL(string: "\(Self.proxyBase)/file/favourites") else { return }

        // First GET the current sha (needed for PUT)
        var sha: String?
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                let ghFile = try JSONDecoder().decode(GitHubFile.self, from: data)
                sha = ghFile.sha
            }
        } catch {}

        let payload = FavouritesPayload(urls: Array(favourites).sorted())
        guard let jsonData = try? JSONEncoder().encode(payload) else { return }
        let base64 = jsonData.base64EncodedString()

        var body: [String: String] = [
            "message": "Update favourites",
            "content": base64,
        ]
        if let sha { body["sha"] = sha }

        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(body)

        _ = try? await URLSession.shared.data(for: request)
    }
}

private struct GitHubFile: Codable {
    let content: String
    let sha: String
}

private struct FavouritesPayload: Codable {
    let urls: [String]
}
