// The tour of the app: its own screens with an explanation, one every
// few seconds, arrows / dots / keys to read one again. Shared by the start screen and the
// installer; expects #tour > #shots, #caption(#ck #ch #cp), #prev #dots #next.
(function () {
const $ = s => document.querySelector(s);
// ---- the tour: the app's own screens (ui/preview/*.webp), one every SLIDE_MS
const SLIDES = [
  ["optimizer", "Optimizer", "Find your best team",
   "Every leader and five members, scored exactly on the chart you pick: your cards as they are, your Holomem Boards planned."],
  ["unitscore", "Unit Score", "Every point explained",
   "The Unit Score the game shows, line by line, and the whole score distribution: expected, max, and how often a run beats it."],
  ["boardplan", "Holomem Board", "What to unlock next",
   "The nodes that lift the team most for the points you hold: what to unlock, what to reset, where each Connect card goes."],
  ["holoboard", "Holomem Board", "Every board at a glance",
   "Each holomem's board as the game draws it, with the points she has earned, spent and left."],
  ["event", "Event", "Borders, read around the clock",
   "Every tier's border every five minutes, where it will end, and the Event Pt you need to finish inside."],
  ["collection", "Collection", "Your whole collection",
   "Every card the game ships at your level, Bloom and Limit Break, with this event's bonus on each."],
  ["gacha", "Gacha", "Your luck, honestly",
   "Every pull tracked and set against the published rates, banner by banner."],
];
const SLIDE_MS = 6000;
let slide = -1, timer = null;
function buildTour() {
  const shots = $("#shots"), dots = $("#dots");
  SLIDES.forEach(([file], i) => {
    const img = document.createElement("img");
    img.src = `preview/${file}.webp`; img.alt = "";
    shots.appendChild(img);
    const d = document.createElement("button");
    d.innerHTML = "<i></i>"; d.setAttribute("aria-label", `Screen ${i + 1}`);
    d.onclick = () => go(i);
    dots.appendChild(d);
  });
  $("#prev").onclick = () => go(slide - 1);
  $("#next").onclick = () => go(slide + 1);
  const tour = $("#tour");
  tour.onmouseenter = () => { tour.classList.add("paused"); clearTimeout(timer); };
  tour.onmouseleave = () => { tour.classList.remove("paused"); go(slide); };
  document.addEventListener("keydown", e => {
    if (e.target.closest("input,textarea")) return;
    if (e.key === "ArrowLeft") go(slide - 1);
    if (e.key === "ArrowRight") go(slide + 1);
  });
  go(0);
}
function go(i) {
  const n = SLIDES.length, prev = slide;
  slide = (i + n) % n;
  clearTimeout(timer);
  document.querySelectorAll("#shots img").forEach((im, k) => im.classList.toggle("on", k === slide));
  document.querySelectorAll("#dots button").forEach((d, k) => {
    d.classList.toggle("seen", k < slide);
    d.classList.remove("on"); void d.offsetWidth;          // restart the fill animation
    d.classList.toggle("on", k === slide);
  });
  const cap = $("#caption"), [, k, h, p] = SLIDES[slide];
  const put = () => { $("#ck").textContent = k; $("#ch").textContent = h; $("#cp").textContent = p; cap.classList.remove("swap"); };
  if (prev === -1 || prev === slide) put(); else { cap.classList.add("swap"); setTimeout(put, 280); }
  if (!$("#tour").classList.contains("paused")) timer = setTimeout(() => go(slide + 1), SLIDE_MS);
}
window.HBTour = { build: buildTour, go };
})();
