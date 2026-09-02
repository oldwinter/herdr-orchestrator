function timelineContinuityElements(timeline) {
  return [...timeline.querySelectorAll("[data-event-id]")];
}

function captureTimelineContinuity(timeline) {
  const elements = timelineContinuityElements(timeline);
  const visible = elements.flatMap((element) => {
    const rect = element.getBoundingClientRect();
    return rect.bottom > 0 && rect.top < innerHeight
      ? [{ element, rect }]
      : [];
  });
  const positions = new Map(
    visible.map(({ element, rect }) => [
      element.dataset.eventId,
      { left: rect.left, top: rect.top },
    ]),
  );
  const newestRect = elements[0]?.getBoundingClientRect();
  const anchor = newestRect?.bottom <= 0 ? visible[0] : null;
  return {
    readingAnchor: anchor
      ? { eventId: anchor.element.dataset.eventId, top: anchor.rect.top }
      : null,
    positions,
  };
}

function restoreTimelineContinuity(timeline, capture, { animate }) {
  if (!capture) return;
  const elementsById = new Map(
    timelineContinuityElements(timeline).map((element) => [
      element.dataset.eventId,
      element,
    ]),
  );
  const anchor = capture.readingAnchor
    ? elementsById.get(capture.readingAnchor.eventId)
    : null;
  if (anchor) {
    const delta = anchor.getBoundingClientRect().top - capture.readingAnchor.top;
    if (Math.abs(delta) > 0.5) {
      window.scrollBy({ top: delta, left: 0, behavior: "instant" });
    }
  }
  if (!animate) return;
  capture.positions.forEach((position, eventId) => {
    const element = elementsById.get(eventId);
    if (!element || typeof element.animate !== "function") return;
    const rect = element.getBoundingClientRect();
    const deltaX = position.left - rect.left;
    const deltaY = position.top - rect.top;
    if (Math.abs(deltaX) <= 0.5 && Math.abs(deltaY) <= 0.5) return;
    const distance = Math.hypot(deltaX, deltaY);
    const animation = element.animate(
      [
        { transform: `translate(${deltaX}px, ${deltaY}px)` },
        { transform: "translate(0px, 0px)" },
      ],
      {
        duration: Math.round(Math.min(420, 220 + distance * 0.15)),
        easing: "cubic-bezier(0.16, 1, 0.3, 1)",
        composite: "replace",
      },
    );
    animation.id = "timeline-continuity";
  });
}
