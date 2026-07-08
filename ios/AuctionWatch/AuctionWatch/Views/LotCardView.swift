import SwiftUI

struct LotCardView: View {
    let lot: Lot
    @Environment(FavouritesService.self) private var favouritesService

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if let urlStr = lot.imageUrl, let url = URL(string: urlStr) {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().aspectRatio(contentMode: .fill)
                    case .failure:
                        Rectangle().fill(.gray.opacity(0.2))
                            .overlay { Image(systemName: "photo").foregroundStyle(.secondary) }
                    default:
                        Rectangle().fill(.gray.opacity(0.1))
                    }
                }
                .frame(width: 80, height: 80)
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(lot.artist).font(.headline)
                Text(lot.title).font(.subheadline).lineLimit(2).foregroundStyle(.secondary)
                Text(lot.house).font(.caption).foregroundStyle(.tertiary)

                HStack {
                    Text(lot.formattedEstimate).font(.caption).bold()
                    Spacer()
                    Text(lot.relativeCloseDate).font(.caption2).foregroundStyle(.secondary)
                }
            }

            Button {
                favouritesService.toggle(lot)
            } label: {
                Image(systemName: favouritesService.isFavourite(lot) ? "heart.fill" : "heart")
                    .foregroundStyle(favouritesService.isFavourite(lot) ? .red : .secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 4)
    }
}
