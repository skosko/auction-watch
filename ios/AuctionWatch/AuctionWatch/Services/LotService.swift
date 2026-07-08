import Foundation

@Observable
final class LotService {
    private(set) var lots: [Lot] = []
    private(set) var isLoading = false
    private(set) var error: String?

    private static let feedURL = URL(string: "https://skosko.github.io/auction-watch/lots.json")!

    var artists: [String] {
        Array(Set(lots.map(\.artist))).sorted()
    }

    func fetch() async {
        isLoading = true
        error = nil
        defer { isLoading = false }

        do {
            let (data, _) = try await URLSession.shared.data(from: Self.feedURL)
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .iso8601
            let feed = try decoder.decode(LotFeed.self, from: data)
            lots = feed.lots
        } catch {
            self.error = error.localizedDescription
        }
    }
}
