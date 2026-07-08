import SwiftUI

enum SortOrder: String, CaseIterable {
    case date = "Date"
    case priceLow = "Price (low)"
    case priceHigh = "Price (high)"
}

struct LotListView: View {
    @Environment(LotService.self) private var lotService
    @Environment(FavouritesService.self) private var favouritesService

    @State private var searchText = ""
    @State private var selectedArtists: Set<String> = []
    @State private var sortOrder: SortOrder = .date
    @State private var showFavouritesOnly = false

    private var filteredLots: [Lot] {
        var result = lotService.lots

        if showFavouritesOnly {
            result = result.filter { favouritesService.isFavourite($0) }
        }

        if !selectedArtists.isEmpty {
            result = result.filter { selectedArtists.contains($0.artist) }
        }

        if !searchText.isEmpty {
            let query = searchText.lowercased()
            result = result.filter {
                $0.artist.lowercased().contains(query) ||
                $0.title.lowercased().contains(query) ||
                $0.house.lowercased().contains(query)
            }
        }

        switch sortOrder {
        case .date:
            result.sort { ($0.closeDate ?? .distantFuture) < ($1.closeDate ?? .distantFuture) }
        case .priceLow:
            result.sort { ($0.estimateEur ?? 0) < ($1.estimateEur ?? 0) }
        case .priceHigh:
            result.sort { ($0.estimateEur ?? 0) > ($1.estimateEur ?? 0) }
        }

        return result
    }

    var body: some View {
        List {
            FilterBar(
                artists: lotService.artists,
                selectedArtists: $selectedArtists,
                sortOrder: $sortOrder,
                showFavouritesOnly: $showFavouritesOnly
            )
            .listRowSeparator(.hidden)
            .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))

            ForEach(filteredLots) { lot in
                NavigationLink(value: lot.id) {
                    LotCardView(lot: lot)
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle("Auction Watch")
        .navigationDestination(for: String.self) { lotId in
            if let lot = lotService.lots.first(where: { $0.id == lotId }) {
                LotDetailView(lot: lot)
            }
        }
        .searchable(text: $searchText, prompt: "Search lots")
        .refreshable {
            await lotService.fetch()
        }
        .task {
            if lotService.lots.isEmpty {
                await lotService.fetch()
            }
        }
        .overlay {
            if lotService.isLoading && lotService.lots.isEmpty {
                ProgressView("Loading lots...")
            } else if let error = lotService.error, lotService.lots.isEmpty {
                ContentUnavailableView("Failed to load", systemImage: "exclamationmark.triangle", description: Text(error))
            }
        }
    }
}
