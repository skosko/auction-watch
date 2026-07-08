import SwiftUI

struct LotDetailView: View {
    let lot: Lot
    @Environment(FavouritesService.self) private var favouritesService

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let urlStr = lot.imageUrl, let url = URL(string: urlStr) {
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image):
                            image.resizable().aspectRatio(contentMode: .fit)
                        case .failure:
                            Rectangle().fill(.gray.opacity(0.2))
                                .aspectRatio(4/3, contentMode: .fit)
                                .overlay { Image(systemName: "photo").font(.largeTitle).foregroundStyle(.secondary) }
                        default:
                            Rectangle().fill(.gray.opacity(0.1))
                                .aspectRatio(4/3, contentMode: .fit)
                                .overlay { ProgressView() }
                        }
                    }
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text(lot.artist).font(.title2).bold()
                    Text(lot.title).font(.title3).foregroundStyle(.secondary)

                    Divider()

                    LabeledContent("House", value: lot.house)
                    LabeledContent("Estimate", value: lot.formattedEstimate)

                    if let date = lot.closeDate {
                        LabeledContent("Closes") {
                            Text(date, style: .date) + Text(" ") + Text(date, style: .time)
                        }
                        LabeledContent("", value: lot.relativeCloseDate)
                    }

                    if let dims = lot.dimensions {
                        LabeledContent("Dimensions", value: dims)
                    }

                    LabeledContent("Source", value: lot.source)
                }
                .padding(.horizontal)

                HStack(spacing: 16) {
                    Button {
                        favouritesService.toggle(lot)
                    } label: {
                        Label(
                            favouritesService.isFavourite(lot) ? "Unfavourite" : "Favourite",
                            systemImage: favouritesService.isFavourite(lot) ? "heart.fill" : "heart"
                        )
                    }
                    .tint(favouritesService.isFavourite(lot) ? .red : .accentColor)

                    Spacer()

                    if let url = URL(string: lot.url) {
                        Link(destination: url) {
                            Label("View on site", systemImage: "safari")
                        }
                    }
                }
                .buttonStyle(.borderedProminent)
                .padding(.horizontal)
            }
            .padding(.vertical)
        }
        .navigationTitle("Lot Detail")
        .navigationBarTitleDisplayMode(.inline)
    }
}
