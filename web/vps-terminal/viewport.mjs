// Compute in layout-viewport CSS pixels. The caller only passes a rendered
// bottom dock; it owns DOM sampling and frame scheduling.
const finite = (value, fallback = 0) => (Number.isFinite(value) ? value : fallback);
const nonNegative = (value, fallback = 0) => Math.max(0, finite(value, fallback));

const WIDTH_RESET_PX = 24;
const KEYBOARD_MIN_PX = 80;
const KEYBOARD_HEIGHT_FRACTION = 0.12;
const KEYBOARD_EXIT_FRACTION = 0.55;
const ADDRESS_BAR_DEADBAND_PX = 14;

export function computeTerminalViewport(
  {
    innerHeight,
    innerWidth,
    visualViewport,
    workspaceRect = {},
    dockRect,
    expanded = false,
    editableFocused = false,
    keyboardEligible = true,
    orientationKey,
    gap = 12,
  },
  previous = null,
) {
  const layoutHeight = nonNegative(innerHeight);
  const screenWidth = nonNegative(innerWidth);
  const height = nonNegative(visualViewport?.height, layoutHeight) || layoutHeight;
  const width = nonNegative(visualViewport?.width, screenWidth) || screenWidth;
  const zoomed = Math.abs(finite(visualViewport?.scale, 1) - 1) > 0.02;
  const stable =
    !!previous &&
    Math.abs(screenWidth - previous.screenWidth) < WIDTH_RESET_PX &&
    (orientationKey == null ||
      previous.orientationKey == null ||
      orientationKey === previous.orientationKey);
  const focused = keyboardEligible && editableFocused;
  // Blur must end compact mode even while zoomed. Don't learn keyboard size
  // or send a new PTY size when the user is just pinching to read.
  if (zoomed && stable)
    return {
      ...previous,
      screenWidth,
      orientationKey,
      zoomed,
      keyboardOpen: focused && previous.keyboardOpen,
    };
  const previousBaseline = stable ? previous.baselineHeight : 0;
  const baselineHeight = focused ? Math.max(previousBaseline || layoutHeight, height) : height;
  const threshold =
    Math.max(KEYBOARD_MIN_PX, baselineHeight * KEYBOARD_HEIGHT_FRACTION) *
    (stable && previous.keyboardOpen ? KEYBOARD_EXIT_FRACTION : 1);
  // On Android the layout viewport can shrink with the visual viewport. Its
  // previous no-keyboard height is the second independent measurement.
  const keyboardOpen =
    !zoomed && focused && Math.max(layoutHeight, baselineHeight) - height >= threshold;
  const viewTop = finite(visualViewport?.offsetTop);
  const viewLeft = finite(visualViewport?.offsetLeft);
  const top = Math.max(viewTop, finite(workspaceRect.top, viewTop));
  const left = Math.max(viewLeft, finite(workspaceRect.left, viewLeft));
  const right = Math.min(viewLeft + width, finite(workspaceRect.right, viewLeft + width));
  let bottom = viewTop + height;
  if (
    !expanded &&
    dockRect &&
    right > left &&
    dockRect.left < right &&
    dockRect.right > left &&
    dockRect.top >= top &&
    dockRect.bottom > dockRect.top
  )
    bottom = Math.min(bottom, dockRect.top);
  const rawAvailableHeight = Math.max(0, Math.floor(bottom - top - nonNegative(gap)));
  const heightDelta =
    stable && Number.isFinite(previous?.availableHeight)
      ? Math.abs(rawAvailableHeight - previous.availableHeight)
      : 0;
  const keyboardTransition =
    !!keyboardEligible &&
    stable &&
    (previous.keyboardOpen !== keyboardOpen || heightDelta >= KEYBOARD_MIN_PX);
  const suppressJitter =
    !!keyboardEligible &&
    stable &&
    !keyboardTransition &&
    previous.expanded === expanded &&
    Math.abs(width - finite(previous.viewportWidth, width)) < 1 &&
    rawAvailableHeight > 0 &&
    previous.availableHeight > 0 &&
    heightDelta > 0 &&
    heightDelta < ADDRESS_BAR_DEADBAND_PX;
  return {
    availableHeight: suppressJitter ? previous.availableHeight : rawAvailableHeight,
    keyboardOpen,
    keyboardTransition,
    baselineHeight,
    screenWidth,
    viewportWidth: width,
    expanded,
    orientationKey,
    zoomed,
  };
}
