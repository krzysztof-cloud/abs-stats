// Theme selection. Runs in <head> before CSS so the page never flashes the wrong theme.
(function () {
  "use strict";
  var THEMES = [
    { group: "Automatic", items: [
      ["system", "Match system (dark/light)"],
      ["seasonal", "Seasonal (holiday themes on their dates)"],
    ] },
    { group: "Themes", items: [
      ["dusk", "Dusk"], ["daylight", "Daylight"], ["sepia", "Sepia"], ["midnight", "Midnight"],
      ["forest", "Forest"], ["rose", "Rose"], ["oled", "OLED black"],
    ] },
    { group: "Holidays", items: [
      ["lunar", "Lunar New Year"], ["valentine", "Valentine's Day"], ["stpatrick", "St. Patrick's Day"],
      ["fireworks", "Canada Day / July 4th"], ["halloween", "Halloween"], ["harvest", "Thanksgiving"],
      ["winter", "Winter holidays"],
    ] },
  ];
  // Lunar New Year moves every year, so its dates are listed.
  var LUNAR = { 2026: [2, 17], 2027: [2, 6], 2028: [1, 26], 2029: [2, 13], 2030: [2, 3], 2031: [1, 23], 2032: [2, 11] };

  function holidayFor(d) {
    var y = d.getFullYear(), md = (d.getMonth() + 1) * 100 + d.getDate();
    var lunar = LUNAR[y];
    if (lunar) {
      var start = new Date(y, lunar[0] - 1, lunar[1] - 3), end = new Date(y, lunar[0] - 1, lunar[1] + 7);
      if (d >= start && d <= end) return "lunar";
    }
    if (md >= 207 && md <= 214) return "valentine";
    if (md >= 310 && md <= 317) return "stpatrick";
    if (md >= 628 && md <= 705) return "fireworks";
    if (md >= 1015 && md <= 1031) return "halloween";
    if (md >= 1101 && md <= 1130) return "harvest";
    if (md >= 1201 || md <= 101) return "winter";
    return null;
  }

  function systemTheme() {
    try { return window.matchMedia("(prefers-color-scheme: light)").matches ? "daylight" : "dusk"; } catch (e) { return "dusk"; }
  }

  function resolve(choice) {
    if (choice === "system") return systemTheme();
    if (choice === "seasonal") return holidayFor(new Date()) || systemTheme();
    return choice;
  }

  var choice = "dusk";
  try { choice = localStorage.getItem("abs-theme") || choice; } catch (e) {}

  function apply() {
    document.documentElement.dataset.theme = resolve(choice);
  }

  window.absTheme = {
    groups: THEMES,
    get choice() { return choice; },
    get resolved() { return document.documentElement.dataset.theme; },
    set: function (c) {
      choice = c;
      try { localStorage.setItem("abs-theme", c); } catch (e) {}
      apply();
    },
  };
  apply();
})();
