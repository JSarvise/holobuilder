// HBFmt: the one formatter every Night Deck page uses for dates, numbers and missing values,
// for cross-page consistency. Classic script: load it in <head> with
// <script src="/hb-format.js"></script>; it sets window.HBFmt for classic and module scripts alike.
//
// Rules:
// - Dates: "15 Oct 19:04" (day, short month, 24 h), the year only when it is not this year
//   ("7 Oct 2025 19:04"). A day without a time: "15 Oct". Times: "19:04". Local time zone,
//   unless {utc:true}.
// - Numbers: exact with thousands separators ("1,909,260"). A compact form exists ("3.67 M",
//   "58 k") for places that label it as such; the unit is always set off by one no-break space.
// - Missing values: one placeholder, HBFmt.NA ("—"), everywhere.
(function () {
  "use strict";
  const NA = "—";
  const SP = " ";   // the one space between a figure and its unit (no-break)
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const ok = n => typeof n === "number" && isFinite(n);
  const p2 = n => String(n).padStart(2, "0");

  // Anything time-like -> Date. Numbers: epoch seconds or milliseconds (the game's times are ms,
  // the app's own sync stamps are s; anything above 1e12 is ms). Strings: ISO, or a bare "YYYY-MM-DD" day,
  // which is read as that calendar day (not midnight UTC shifted into yesterday).
  function toDate(t) {
    if (t == null || t === "") return null;
    if (t instanceof Date) return isNaN(t) ? null : t;
    if (typeof t === "number") {
      if (!isFinite(t) || t === 0) return null;
      return new Date(Math.abs(t) > 1e12 ? t : t * 1000);
    }
    const s = String(t);
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
    return isNaN(d) ? null : d;
  }
  function parts(d, utc) {
    return utc
      ? { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes() }
      : { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), h: d.getHours(), mi: d.getMinutes() };
  }
  // "15 Oct" (+ " 2025" when not this year, or always with {year:true})
  function day(t, o = {}) {
    const d = toDate(t); if (!d) return NA;
    const p = parts(d, o.utc), now = new Date().getFullYear();
    return `${p.d} ${MON[p.mo]}${o.year || p.y !== now ? " " + p.y : ""}`;
  }
  function time(t, o = {}) {
    const d = toDate(t); if (!d) return NA;
    const p = parts(d, o.utc);
    return `${p2(p.h)}:${p2(p.mi)}`;
  }
  // "15 Oct 19:04"
  function date(t, o = {}) {
    const d = toDate(t); if (!d) return NA;
    return `${day(d, o)} ${time(d, o)}`;
  }
  // "7 Oct to 19 Oct" (one year shown once, at the end, when not this year)
  function range(a, b, o = {}) {
    const da = toDate(a), db = toDate(b);
    if (!da && !db) return NA;
    if (!da) return `until ${(o.time ? date : day)(db, o)}`;
    if (!db) return `since ${(o.time ? date : day)(da, o)}`;
    return `${(o.time ? date : day)(da, o)} to ${(o.time ? date : day)(db, o)}`;
  }
  // "today" / "yesterday" / "3 d ago" / "5 h ago" / "12 min ago"
  function ago(t) {
    const d = toDate(t); if (!d) return NA;
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}${SP}min ago`;
    if (s < 86400) return `${Math.round(s / 3600)}${SP}h ago`;
    const dd = Math.round(s / 86400);
    return dd === 1 ? "yesterday" : `${dd}${SP}d ago`;
  }

  // 1909260 -> "1,909,260"; {d:1} keeps one decimal ("12.5")
  function num(n, o = {}) {
    if (!ok(n)) return NA;
    const d = o.d || 0;
    return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  // +1,909,260 / -12 / 0
  function signed(n, o = {}) {
    if (!ok(n)) return NA;
    return (n > 0 ? "+" : n < 0 ? "-" : "") + num(Math.abs(n), o);
  }
  // 3,670,000 -> "3.67 M", 58,000 -> "58 k", under 10,000 exact. Label the column/line as rounded.
  function compact(n) {
    if (!ok(n)) return NA;
    const a = Math.abs(n), s = n < 0 ? "-" : "";
    if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)}${SP}M`;
    if (a >= 1e4) return `${s}${Math.round(a / 1e3)}${SP}k`;
    return s + num(a);
  }
  // 0.0512 -> "5.1 %"; {d:0} -> "5 %"
  function pct(x, o = {}) {
    if (!ok(x)) return NA;
    const d = o.d == null ? 1 : o.d;
    return `${(x * 100).toFixed(d)}${SP}%`;
  }
  // "3 h 20 min" from seconds
  function dur(sec) {
    if (!ok(sec)) return NA;
    const m = Math.round(sec / 60);
    if (m < 60) return `${m}${SP}min`;
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h}${SP}h ${r}${SP}min` : `${h}${SP}h`;
  }
  // a value or the placeholder
  const or = v => (v == null || v === "" || (typeof v === "number" && !isFinite(v)) ? NA : String(v));

  window.HBFmt = { NA, SP, toDate, day, time, date, range, ago, num, signed, compact, pct, dur, or };
})();
