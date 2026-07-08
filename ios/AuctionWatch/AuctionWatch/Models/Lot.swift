import Foundation

struct LotFeed: Codable {
    let generatedAt: String
    let lots: [Lot]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case lots
    }
}

struct Lot: Codable, Identifiable {
    let source: String
    let artist: String
    let title: String
    let house: String
    let closeDate: Date?
    let url: String
    let imageUrl: String?
    let estimateLow: Int?
    let estimateHigh: Int?
    let currency: String?
    let dimensions: String?
    let estimateEur: Int?

    var id: String { url }

    enum CodingKeys: String, CodingKey {
        case source, artist, title, house, url, currency, dimensions
        case closeDate = "close_date"
        case imageUrl = "image_url"
        case estimateLow = "estimate_low"
        case estimateHigh = "estimate_high"
        case estimateEur = "estimate_eur"
    }

    var formattedEstimate: String {
        guard estimateLow != nil || estimateHigh != nil else {
            return "Estimate not published"
        }
        let cur = currency ?? ""
        if let lo = estimateLow, let hi = estimateHigh {
            let (low, high) = lo < hi ? (lo, hi) : (hi, lo)
            return "\(cur)\(low.formatted()) - \(high.formatted())"
        }
        let val = estimateLow ?? estimateHigh ?? 0
        return "\(cur)\(val.formatted())"
    }

    var relativeCloseDate: String {
        guard let date = closeDate else { return "No date" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: .now)
    }
}
