import Foundation
import CoreGraphics
import CoreText
let url = URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL
var box = CGRect(x: 0, y: 0, width: 612, height: 792)
let ctx = CGContext(url, mediaBox: &box, nil)!
ctx.beginPDFPage(nil)
let lines: [(String, String, CGFloat, CGFloat)] = [
  ("Invoice number 2024-117 for Acme Corp", "ArialMT", 14, 700),
  ("Total due: 1,250.00 USD", "TimesNewRomanPS-BoldMT", 14, 680),
  ("Thanks for your business.", "Helvetica", 12, 662),
  ("Fine print in Courier", "Courier", 10, 648),
]
for (text, fontName, size, y) in lines {
  let font = CTFontCreateWithName(fontName as CFString, size, nil)
  let attrs: [NSAttributedString.Key: Any] = [NSAttributedString.Key(kCTFontAttributeName as String): font]
  let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attrs))
  ctx.textPosition = CGPoint(x: 72, y: y)
  CTLineDraw(line, ctx)
}
ctx.endPDFPage()
ctx.closePDF()
