"""Rendered geometry assertions. Call only after real route content is ready.

No Hub is started here. Helpers fail for absent/hidden targets rather than
vacuously passing. Tolerance is CSS pixels, not device pixels. These supplement
screenshots and functional tests, and deliberately do not equate non-overflow
with good alignment.
"""
import json


def geometry(locator):
    assert locator.count() == 1, "Geometry target must be unique"
    assert locator.is_visible(), "Geometry target must be visible"
    return locator.evaluate("""n => {
      const r=n.getBoundingClientRect(), s=getComputedStyle(n), f=v=>parseFloat(v)||0;
      return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,
        width:r.width,height:r.height,cx:(r.left+r.right)/2,cy:(r.top+r.bottom)/2,
        contentLeft:r.left+f(s.borderLeftWidth)+f(s.paddingLeft),
        contentRight:r.right-f(s.borderRightWidth)-f(s.paddingRight),
        paddingLeft:f(s.paddingLeft),paddingRight:f(s.paddingRight)};
    }""")


def close(actual, expected, label, tolerance=1):
    assert abs(actual-expected) <= tolerance, (
        f"{label}: {actual:.2f} vs {expected:.2f}; tolerance {tolerance}px")


def assert_content_rails(*locators):
    """Compare card header/body/footer content edges, not variable text widths."""
    assert len(locators) >= 2
    boxes = [geometry(n) for n in locators]
    for item in boxes[1:]:
        for edge in ("contentLeft", "contentRight"):
            close(item[edge], boxes[0][edge], edge)
    return boxes


def assert_same_row(*locators):
    """Control box centerline; this is NOT a font glyph baseline measurement."""
    assert len(locators) >= 2
    boxes = [geometry(n) for n in locators]
    for item in boxes[1:]:
        close(item["cy"], boxes[0]["cy"], "control centerline")
    for a, b in zip(boxes, boxes[1:]):
        assert a["right"] <= b["left"]+1, ("overlapping row controls", boxes)
    return boxes


def assert_two_by_two(items):
    assert items.count() == 4, "Expected exactly four visible tool entries"
    boxes = [geometry(items.nth(i)) for i in range(4)]
    for item in boxes[1:]:
        close(item["width"], boxes[0]["width"], "equal tool column widths")
    for a, b in ((0, 1), (2, 3)):
        close(boxes[a]["cy"], boxes[b]["cy"], "tool row centerline")
        assert boxes[a]["right"] < boxes[b]["left"], boxes
    for a, b in ((0, 2), (1, 3)):
        close(boxes[a]["left"], boxes[b]["left"], "tool column left")
        assert boxes[a]["bottom"] < boxes[b]["top"], boxes
    return boxes


def assert_page_heading_rail(page):
    content = geometry(page.locator("#page"))
    title = geometry(page.locator("#page h1").first)
    close(title["left"], content["contentLeft"], "page title left rail")
    return content, title


def assert_mobile_dock_rail(page):
    content = geometry(page.locator("#page"))
    dock = geometry(page.locator(".mobile-dock"))
    close(dock["left"], content["contentLeft"], "dock/page left rail")
    close(dock["right"], content["contentRight"], "dock/page right rail")
    assert dock["top"] >= 0 and dock["bottom"] <= page.evaluate(
        "visualViewport ? visualViewport.height + visualViewport.offsetTop : innerHeight"
    )+1, dock
    return content, dock


def assert_summary_pair(first, second):
    boxes = assert_content_rails(first, second)
    close(boxes[0]["height"], boxes[1]["height"], "peer summary heights")
    return boxes


def write_metrics(path, metrics):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
