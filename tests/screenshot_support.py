"""Single-attempt screenshots with synthetic layout context on failure."""


def capture_route_screenshot(page, path, *, route, scheme, viewport, document_size):
    """Keep full-page capture and its original error; never retry or downgrade."""
    try:
        return page.screenshot(path=str(path), full_page=True)
    except Exception as error:
        BaseException.add_note(error,
            f'Route screenshot context: route={route}; scheme={scheme}; '
            f'viewport={viewport[0]}x{viewport[1]}; '
            f'document={document_size[0]}x{document_size[1]}')
        raise
