// Pulls page: the small amount of statistics the luck panel needs. Pure functions, no DOM.
//
// What the server gives per rarity is an OBSERVED count k and an EXPECTED count E over n
// compared pulls (gacha_view.py: E already applies the 9 + 1 guaranteed slot of a 10-pull).
// The exact law of k is a Poisson-binomial (the guaranteed slot draws from another table),
// which the API does not expose draw by draw. We use the binomial with the SAME MEAN
// (p = E / n). Its variance is never smaller than the true one, so the ranges drawn from it
// are slightly WIDER than the truth: conservative, never falsely precise. The page says so.

// log of n choose k, by summing logs (n stays in the hundreds here)
function logChoose(n, k) {
  if (k < 0 || k > n) return -Infinity;
  k = Math.min(k, n - k);
  let s = 0;
  for (let i = 1; i <= k; i++) s += Math.log(n - k + i) - Math.log(i);
  return s;
}

// P(X = k) for X ~ Binomial(n, p)
export function binomPmf(n, p, k) {
  if (k < 0 || k > n) return 0;
  if (p <= 0) return k === 0 ? 1 : 0;
  if (p >= 1) return k === n ? 1 : 0;
  return Math.exp(logChoose(n, k) + k * Math.log(p) + (n - k) * Math.log(1 - p));
}

// the whole distribution, so callers can take quantiles and tails from one pass
export function binomPmfs(n, p) {
  const out = new Array(n + 1);
  for (let k = 0; k <= n; k++) out[k] = binomPmf(n, p, k);
  return out;
}

// smallest k with P(X <= k) >= q
export function binomQuantile(pmfs, q) {
  let c = 0;
  for (let k = 0; k < pmfs.length; k++) {
    c += pmfs[k];
    if (c >= q - 1e-12) return k;
  }
  return pmfs.length - 1;
}

// mid-p percentile of an observed k: P(X < k) + P(X = k) / 2, in 0..1.
// "0.22" reads as: 22 % of players making exactly these pulls would get fewer.
export function midPercentile(pmfs, k) {
  let below = 0;
  for (let i = 0; i < Math.min(k, pmfs.length); i++) below += pmfs[i];
  return below + (pmfs[k] || 0) / 2;
}

// Wilson score interval for a proportion k / n (z = 1.96 -> 95 %)
export function wilson(k, n, z = 1.96) {
  if (!n) return [0, 1];
  const ph = k / n, z2 = z * z;
  const den = 1 + z2 / n;
  const mid = (ph + z2 / (2 * n)) / den;
  const half = (z * Math.sqrt(ph * (1 - ph) / n + z2 / (4 * n * n))) / den;
  return [Math.max(0, mid - half), Math.min(1, mid + half)];
}

// One rarity row of the luck panel. Returns null when there is no expectation to compare to.
export function luckRow(k, expected, n) {
  if (expected == null || !n) return null;
  const p = Math.min(1, Math.max(0, expected / n));
  const pmfs = binomPmfs(n, p);
  return {
    k, n, expected, p,
    lo: binomQuantile(pmfs, 0.05),        // 90 % of players with these pulls land in lo..hi
    hi: binomQuantile(pmfs, 0.95),
    pct: midPercentile(pmfs, k),
    below: pmfs.slice(0, k).reduce((a, x) => a + x, 0),        // P(X < k)
    above: pmfs.slice(k + 1).reduce((a, x) => a + x, 0),       // P(X > k)
    rate: k / n,
    rateCI: wilson(k, n),
  };
}
