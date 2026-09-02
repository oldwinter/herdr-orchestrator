"use strict";

let sourceWarningResizeState = {
  frame: null,
  cleanup: null,
  operation: null,
};

function sourceWarningIntrinsicHeight(warning) {
  const marginBottom = Number.parseFloat(getComputedStyle(warning).marginBottom);
  return warning.getBoundingClientRect().height + (
    Number.isFinite(marginBottom) ? marginBottom : 0
  );
}

function cancelSourceWarningResize() {
  if (sourceWarningResizeState.frame !== null) {
    cancelAnimationFrame(sourceWarningResizeState.frame);
  }
  sourceWarningResizeState.cleanup?.();
  sourceWarningResizeState = { frame: null, cleanup: null, operation: null };
}

function animateSourceWarningResize(clip, currentHeight, nextHeight) {
  if (!motionAllowed() || Math.abs(currentHeight - nextHeight) < 0.5) {
    clip.style.removeProperty("height");
    return;
  }
  const operation = {};
  const cleanup = () => {
    clip.removeEventListener("transitionend", onEnd);
    clip.style.removeProperty("height");
  };
  const onEnd = (event) => {
    if (
      sourceWarningResizeState.operation === operation
      && event.target === clip
      && event.propertyName === "height"
    ) {
      cleanup();
      sourceWarningResizeState = { frame: null, cleanup: null, operation: null };
    }
  };
  clip.addEventListener("transitionend", onEnd);
  sourceWarningResizeState = { frame: null, cleanup, operation };
  clip.style.height = `${currentHeight}px`;
  void clip.offsetHeight;
  sourceWarningResizeState.frame = requestAnimationFrame(() => {
    if (sourceWarningResizeState.operation !== operation) return;
    sourceWarningResizeState.frame = null;
    clip.style.height = `${nextHeight}px`;
  });
}

function setSourceWarning(message) {
  const region = byId("source-warning-region");
  const clip = byId("source-warning-clip");
  const warning = byId("source-warning");
  const wasVisible = !region.classList.contains("is-hidden");
  const messageChanged = warning.textContent !== (message || "");
  if (!messageChanged && wasVisible === Boolean(message)) return;
  const currentHeight = wasVisible ? clip.getBoundingClientRect().height : 0;
  cancelSourceWarningResize();
  if (message) {
    if (wasVisible && messageChanged && motionAllowed()) {
      clip.style.height = `${currentHeight}px`;
      void clip.offsetHeight;
    }
    warning.textContent = message;
    region.removeAttribute("aria-hidden");
    region.classList.remove("is-hidden");
    if (wasVisible && messageChanged) {
      animateSourceWarningResize(
        clip,
        currentHeight,
        sourceWarningIntrinsicHeight(warning),
      );
    } else {
      clip.style.removeProperty("height");
    }
    return;
  }
  region.setAttribute("aria-hidden", "true");
  region.classList.add("is-hidden");
}
