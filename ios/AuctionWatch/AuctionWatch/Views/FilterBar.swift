import SwiftUI

struct FilterBar: View {
    let artists: [String]
    @Binding var selectedArtists: Set<String>
    @Binding var sortOrder: SortOrder
    @Binding var showFavouritesOnly: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("Sort", selection: $sortOrder) {
                    ForEach(SortOrder.allCases, id: \.self) { order in
                        Text(order.rawValue).tag(order)
                    }
                }
                .pickerStyle(.menu)

                Spacer()

                Toggle(isOn: $showFavouritesOnly) {
                    Image(systemName: showFavouritesOnly ? "heart.fill" : "heart")
                }
                .toggleStyle(.button)
                .tint(showFavouritesOnly ? .red : .secondary)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(artists, id: \.self) { artist in
                        let isSelected = selectedArtists.contains(artist)
                        Button {
                            if isSelected {
                                selectedArtists.remove(artist)
                            } else {
                                selectedArtists.insert(artist)
                            }
                        } label: {
                            Text(artist)
                                .font(.caption)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 6)
                                .background(isSelected ? Color.accentColor : Color(.systemGray5))
                                .foregroundStyle(isSelected ? .white : .primary)
                                .clipShape(Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal)
            }
        }
    }
}
