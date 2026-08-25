"use strict";

(() => {
  try {
    const theme = localStorage.getItem("owner-control-theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.dataset.theme = theme;
    }
  } catch (_error) {
    // The regular client applies the system preference when storage is unavailable.
  }
})();
