// Leader options (as in the community tool holodori.best): which holomem leads ("all" or one),
// which of her outfits ("the best she has" or one card), and "no leader
// overlap" (no member card of the leader's own holomem). The server filters
// every candidate by them before the develop (`leader_member`, `leader_card`,
// `no_leader_overlap`); a refine swap may not break them either.
import { el } from "/dom.js";

const $ = (id) => document.getElementById(id);
let byMember = new Map();

export function leaderOptions() {
  const member = $("leadermember") ? $("leadermember").value : "";
  const card = $("leaderoutfit") ? $("leaderoutfit").value : "";
  const out = {};
  if (member) out.leader_member = member;
  if (member && card) out.leader_card = card;
  if ($("noleaderoverlap") && $("noleaderoverlap").checked) out.no_leader_overlap = true;
  return out;
}

function fillOutfits() {
  const outfit = $("leaderoutfit");
  const member = $("leadermember").value;
  outfit.replaceChildren(new Option("The best outfit she has", ""));
  for (const card of byMember.get(member) || []) {
    outfit.append(new Option(`${card.costume_name || card.id}${card.rarity ? " · " + card.rarity : ""}`, card.id));
  }
  outfit.disabled = !member;
}

export async function mountLeader() {
  const grid = $("pool") ? $("pool").closest(".ctrl-grid") : null;
  if (!grid || $("leadermember")) return;
  const ctrl = el("div", "ctrl ctrl-wide");
  ctrl.append(el("span", "ctrl-lab", "Leader"));
  const member = document.createElement("select");
  member.id = "leadermember";
  member.append(new Option("All holomembers", ""));
  const outfit = document.createElement("select");
  outfit.id = "leaderoutfit";
  outfit.append(new Option("The best outfit she has", ""));
  outfit.disabled = true;
  const overlap = el("label", "dev-check");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.id = "noleaderoverlap";
  overlap.append(box, document.createTextNode(" No leader overlap (no member card of the leader's holomem)"));
  ctrl.append(member, outfit, overlap,
              el("p", "ctrl-hint", "The leader's outfit skill applies to the team; any holomem picks her best leader card."));
  grid.append(ctrl);
  member.addEventListener("change", fillOutfits);
  try {
    const doc = await (await fetch("/api/collection")).json();
    byMember = new Map();
    for (const card of doc.cards || []) {
      if (!card.owned) continue;
      if (!byMember.has(card.member)) byMember.set(card.member, []);
      byMember.get(card.member).push(card);
    }
    [...byMember.keys()].sort().forEach((name) => member.append(new Option(name, name)));
  } catch (_) { /* the leader stays "all holomembers" */ }
}

mountLeader();
