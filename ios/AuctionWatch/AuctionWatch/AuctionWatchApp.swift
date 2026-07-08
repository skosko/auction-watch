import SwiftUI

@main
struct AuctionWatchApp: App {
    @State private var lotService = LotService()
    @State private var favouritesService = FavouritesService()

    var body: some Scene {
        WindowGroup {
            NavigationSplitView {
                LotListView()
            } detail: {
                Text("Select a lot")
                    .foregroundStyle(.secondary)
            }
            .environment(lotService)
            .environment(favouritesService)
        }
    }
}
