/* The per-song note walk of `hbuilder.domain.song.score_song_detailed`, in C.
 *
 * This is the SEPARABLE fast path only (power order-invariant, no note reachable
 * from two different slots by two different members' SPECIALs) -- the caller
 * checks both conditions in Python, exactly as the Python path does, and keeps
 * the Python walk for the rest.
 *
 * BIT-IDENTITY IS THE CONTRACT. Every expression below is written in the same
 * order as the Python it replaces, on IEEE-754 doubles:
 *   - build with -O2 -ffp-contract=off and WITHOUT -ffast-math, so the compiler
 *     neither fuses a*b+c into an FMA nor re-associates;
 *   - `py_sum` reproduces CPython >= 3.12's `sum()` over floats, which is
 *     Neumaier-compensated and NOT a plain left fold;
 *   - the E[max] walk sorts by magnitude descending with a STABLE sort, as
 *     `sorted(..., key=-magnitude)` is.
 * tools/web/kernel/test_notewalk.py holds it to `==` against the Python.
 *
 * Live sets are 64-bit masks (up to 64 terms): the SPECIAL activation-rate riders
 * add one term per boosted ACTIVE window (`song._rider_terms`), ~10 a team and up
 * to 25 -- past the 32 a uint32 could hold.
 */
#include <math.h>
#include <stdint.h>
#include <string.h>

#define MAX_TERMS 64
#define SLOTS 5
#define MEMO 4096 /* power of two; a team has ~100-1000 distinct live sets */

typedef struct {
    int32_t bucket;      /* first-appearance order decides the summation order */
    int32_t is_boost;    /* joins B (the boost maximum) rather than the ratio */
    int32_t owner;       /* member index owning a ratio term, -1 if none */
    int32_t has_target;  /* a boost with a count-targeted recipient set */
    uint32_t target_mask;/* bit i = member i is a recipient */
    int32_t pad_;
    double magnitude;
    double occupancy;
} Term;

/* CPython's builtin sum() for a list of floats (bltinmodule.c, 3.12+). */
static double py_sum(const double *x, int n) {
    if (n == 0) return 0.0;
    double f = 0.0 + x[0];
    double c = 0.0;
    for (int i = 1; i < n; i++) {
        double v = x[i];
        double t = f + v;
        if (fabs(f) >= fabs(v)) c += (f - t) + v;
        else c += (v - t) + f;
        f = t;
    }
    if (c != 0.0 && isfinite(c)) f += c;
    return f;
}

/* `ExpectedMaximumActiveSkill.combine` over (bucket, magnitude, occupancy),
 * every term non-exclusive (the per-song path always passes exclusive=False). */
static double emax_combine(const int32_t *bucket, const double *mag, const double *occ, int n) {
    int32_t order[MAX_TERMS];
    int nb = 0;
    int32_t seen[MAX_TERMS];
    for (int i = 0; i < n; i++) {
        int found = 0;
        for (int b = 0; b < nb; b++) if (seen[b] == bucket[i]) { found = 1; break; }
        if (!found) seen[nb++] = bucket[i];
    }
    double total = 0.0;
    for (int b = 0; b < nb; b++) {
        int m = 0;
        for (int i = 0; i < n; i++) if (bucket[i] == seen[b]) order[m++] = i;
        /* stable insertion sort, descending magnitude */
        for (int i = 1; i < m; i++) {
            int32_t k = order[i];
            int j = i - 1;
            while (j >= 0 && mag[order[j]] < mag[k]) { order[j + 1] = order[j]; j--; }
            order[j + 1] = k;
        }
        double freep = 1.0, used = 0.0, group = 0.0;
        for (int i = 0; i < m; i++) {
            int32_t k = order[i];
            group += mag[k] * occ[k] * freep * (1.0 - used);
            freep *= 1.0 - occ[k];
        }
        total += group;
    }
    return total;
}

/* `scoring._score_up_pool` for the terms whose bit is set in `live`. */
double hd_pool(const Term *terms, int nterms, uint64_t live, double skill_effect_up) {
    int32_t rb[MAX_TERMS], bb[MAX_TERMS];
    double rm[MAX_TERMS], ro[MAX_TERMS], bm[MAX_TERMS], bo[MAX_TERMS];
    int32_t rowner[MAX_TERMS], btarget[MAX_TERMS];
    uint32_t bmask[MAX_TERMS];
    int nr = 0, nbst = 0, any_target = 0;
    for (int i = 0; i < nterms; i++) {
        if (!((live >> i) & 1u)) continue;
        const Term *t = &terms[i];
        if (t->is_boost) {
            bb[nbst] = t->bucket; bm[nbst] = t->magnitude; bo[nbst] = t->occupancy;
            btarget[nbst] = t->has_target; bmask[nbst] = t->target_mask;
            if (t->has_target) any_target = 1;
            nbst++;
        } else {
            rb[nr] = t->bucket; rm[nr] = t->magnitude; ro[nr] = t->occupancy;
            rowner[nr] = t->owner;
            nr++;
        }
    }
    double scaled[MAX_TERMS];
    if (!any_target) {
        double multiplier = 1.0 + skill_effect_up + emax_combine(bb, bm, bo, nbst);
        for (int i = 0; i < nr; i++) scaled[i] = rm[i] * multiplier;
        return emax_combine(rb, scaled, ro, nr);
    }
    int32_t sb[MAX_TERMS]; double sm[MAX_TERMS], so[MAX_TERMS]; int ns = 0;
    for (int i = 0; i < nbst; i++) if (!btarget[i]) { sb[ns] = bb[i]; sm[ns] = bm[i]; so[ns] = bo[i]; ns++; }
    double shared = 1.0 + skill_effect_up + emax_combine(sb, sm, so, ns);
    for (int i = 0; i < nr; i++) {
        double parts[MAX_TERMS]; int np = 0;
        for (int j = 0; j < nbst; j++) {
            if (!btarget[j]) continue;
            if (rowner[i] >= 0 && ((bmask[j] >> rowner[i]) & 1u)) parts[np++] = bm[j] * bo[j];
        }
        /* Python: shared + sum(generator); sum() of an empty generator is int 0 */
        double multiplier = shared + py_sum(parts, np);
        scaled[i] = rm[i] * multiplier;
    }
    return emax_combine(rb, scaled, ro, nr);
}

typedef struct {
    const Term *terms; int nterms;
    const double *score_rates; int nrates;
    double skill_effect_up;
    uint64_t keys[MEMO]; double vals[MEMO]; uint8_t full[MEMO]; int used;
} Ctx;

/* `_combined(live)`: stacking_model.combine([*score_rates, ratio]) under the
 * additive model, i.e. Python's sum() over that list. Memoised per call. */
static double combined(Ctx *c, uint64_t live) {
    uint32_t h = (uint32_t)((live * 0x9E3779B97F4A7C15ull) >> 52) & (MEMO - 1);
    while (c->full[h]) {
        if (c->keys[h] == live) return c->vals[h];
        h = (h + 1) & (MEMO - 1);
    }
    double buf[MAX_TERMS + 1];
    for (int i = 0; i < c->nrates; i++) buf[i] = c->score_rates[i];
    buf[c->nrates] = hd_pool(c->terms, c->nterms, live, c->skill_effect_up);
    double v = py_sum(buf, c->nrates + 1);
    /* never fill the table: an empty slot must stay to end every probe */
    if (c->used < MEMO * 3 / 4) { c->full[h] = 1; c->keys[h] = live; c->vals[h] = v; c->used++; }
    return v;
}

/* The per-note live-term mask, spread in two steps: every note starts from the
 * always-live terms, then each windowed term ORs its bit over its note-index
 * ranges (`ranges` is n pairs of low, high; half-open). Integer ORs only. */
void hd_bits_fill(uint64_t *bits, int note_count, uint64_t always_mask) {
    for (int i = 0; i < note_count; i++) bits[i] = always_mask;
}

void hd_bits_or(uint64_t *bits, int note_count, uint64_t mask, int n, const int32_t *ranges) {
    for (int k = 0; k < n; k++) {
        int lo = ranges[2 * k], hi = ranges[2 * k + 1];
        if (lo < 0) lo = 0;
        if (hi > note_count) hi = note_count;
        for (int i = lo; i < hi; i++) bits[i] |= mask;
    }
}

/* The separable fast path. `cover` is [nspecial][SLOTS][2] half-open note-index
 * ranges; `special_bit[k]` / `special_member[k]` describe SPECIAL term k.
 * Writes the winning permutation (slot -> member index) and returns the total. */
double hd_walk(int note_count, const double *note_weights, const uint64_t *fixed_bits,
               double constant,
               const Term *terms, int nterms,
               const double *score_rates, int nrates, double skill_effect_up,
               int nspecial, const int32_t *special_bit, const int32_t *special_member,
               const int32_t *cover, int32_t *best_perm, double *base_scratch,
               uint64_t *extra_scratch, const uint8_t *allowed) {
    /* `allowed` (NULL: every order) marks the orders to consider by their index in
     * itertools.permutations(range(5)) order: the orders consistent with one outcome of a
     * count-limited recipients tie (`song._support_tie_outcomes`). NAN if it allows none.
     * The memo is on the stack, ~13 KB: the web server scores in THREADS with the GIL released; a
     * `static` memo here would be shared by them, and concurrent /api/row-detail
     * requests would mix teams' scores */
    Ctx ctx;
    ctx.terms = terms; ctx.nterms = nterms;
    ctx.score_rates = score_rates; ctx.nrates = nrates;
    ctx.skill_effect_up = skill_effect_up;
    memset(ctx.full, 0, sizeof ctx.full);
    ctx.used = 0;

    double fixed_total = 0.0;
    for (int i = 0; i < note_count; i++) {
        double value = ceil(constant * note_weights[i] * (1.0 + combined(&ctx, fixed_bits[i])));
        base_scratch[i] = value;
        fixed_total += value;
    }

    double delta[SLOTS][SLOTS];
    memset(delta, 0, sizeof delta);
    for (int owner = 0; owner < SLOTS; owner++) {
        int has = 0;
        for (int k = 0; k < nspecial; k++) if (special_member[k] == owner) { has = 1; break; }
        if (!has) continue;
        for (int slot = 0; slot < SLOTS; slot++) {
            int lo_all = note_count, hi_all = 0;
            for (int k = 0; k < nspecial; k++) {
                if (special_member[k] != owner) continue;
                int lo = cover[(k * SLOTS + slot) * 2], hi = cover[(k * SLOTS + slot) * 2 + 1];
                if (lo < hi) { if (lo < lo_all) lo_all = lo; if (hi > hi_all) hi_all = hi; }
            }
            if (lo_all >= hi_all) continue;
            memset(extra_scratch + lo_all, 0, (size_t)(hi_all - lo_all) * sizeof(uint64_t));
            for (int k = 0; k < nspecial; k++) {
                if (special_member[k] != owner) continue;
                int lo = cover[(k * SLOTS + slot) * 2], hi = cover[(k * SLOTS + slot) * 2 + 1];
                uint64_t mask = 1ull << special_bit[k];
                for (int i = lo; i < hi; i++) extra_scratch[i] |= mask;
            }
            /* every summand is an integer-valued double far below 2^53, so the
             * order of this sum does not matter */
            double change = 0.0;
            for (int i = lo_all; i < hi_all; i++) {
                if (!extra_scratch[i]) continue;
                change += ceil(constant * note_weights[i]
                               * (1.0 + combined(&ctx, fixed_bits[i] | extra_scratch[i])))
                          - base_scratch[i];
            }
            delta[owner][slot] = change;
        }
    }

    /* itertools.permutations(range(5)) order, strict `>` keeps the first maximum */
    int p[SLOTS] = {0, 1, 2, 3, 4};
    double best = 0.0; int have = 0;
    for (int index = 0;; index++) {
        if (!allowed || allowed[index]) {
            double total = fixed_total;
            for (int slot = 0; slot < SLOTS; slot++) total += delta[p[slot]][slot];
            if (!have || total > best) { best = total; have = 1; for (int s = 0; s < SLOTS; s++) best_perm[s] = p[s]; }
        }
        int i = SLOTS - 2;
        while (i >= 0 && p[i] > p[i + 1]) i--;
        if (i < 0) break;
        int j = SLOTS - 1;
        while (p[j] < p[i]) j--;
        int t = p[i]; p[i] = p[j]; p[j] = t;
        for (int a = i + 1, b = SLOTS - 1; a < b; a++, b--) { t = p[a]; p[a] = p[b]; p[b] = t; }
    }
    return have ? best : NAN;
}

/* The COUPLED case, exactly: a note reachable from two slots by two different members'
 * slot-dependent terms (a SPECIAL, or an ACTIVE window a rider boosts that runs into the
 * next slot's SPECIAL), so the 120 totals do not split into per-slot deltas. Each order is
 * walked over the notes any slot-dependent term can reach -- `song.score_song_detailed`'s
 * own 120-order walk, in C. Every summand is an integer-valued double far below 2^53, so
 * `fixed_total + SUM(ceil(new) - base)` is bit-identical to the walk's full sum. */
double hd_walk_general(int note_count, const double *note_weights, const uint64_t *fixed_bits,
                       double constant,
                       const Term *terms, int nterms,
                       const double *score_rates, int nrates, double skill_effect_up,
                       int nspecial, const int32_t *special_bit, const int32_t *special_member,
                       const int32_t *cover, int32_t *best_perm, double *base_scratch,
                       uint64_t *extra_scratch, const uint8_t *allowed) {
    Ctx ctx;
    ctx.terms = terms; ctx.nterms = nterms;
    ctx.score_rates = score_rates; ctx.nrates = nrates;
    ctx.skill_effect_up = skill_effect_up;
    memset(ctx.full, 0, sizeof ctx.full);
    ctx.used = 0;

    double fixed_total = 0.0;
    for (int i = 0; i < note_count; i++) {
        double value = ceil(constant * note_weights[i] * (1.0 + combined(&ctx, fixed_bits[i])));
        base_scratch[i] = value;
        fixed_total += value;
    }
    int lo_all = note_count, hi_all = 0;
    for (int k = 0; k < nspecial; k++)
        for (int slot = 0; slot < SLOTS; slot++) {
            int lo = cover[(k * SLOTS + slot) * 2], hi = cover[(k * SLOTS + slot) * 2 + 1];
            if (lo < 0) lo = 0;
            if (hi > note_count) hi = note_count;
            if (lo < hi) { if (lo < lo_all) lo_all = lo; if (hi > hi_all) hi_all = hi; }
        }

    int p[SLOTS] = {0, 1, 2, 3, 4};
    double best = 0.0; int have = 0;
    for (int index = 0;; index++) {
        if (allowed && !allowed[index]) goto next_order;
        int slot_of[SLOTS];
        for (int slot = 0; slot < SLOTS; slot++) slot_of[p[slot]] = slot;
        double total = fixed_total;
        if (lo_all < hi_all) {
            memset(extra_scratch + lo_all, 0, (size_t)(hi_all - lo_all) * sizeof(uint64_t));
            for (int k = 0; k < nspecial; k++) {
                int slot = slot_of[special_member[k]];
                int lo = cover[(k * SLOTS + slot) * 2], hi = cover[(k * SLOTS + slot) * 2 + 1];
                if (lo < 0) lo = 0;
                if (hi > note_count) hi = note_count;
                uint64_t mask = 1ull << special_bit[k];
                for (int i = lo; i < hi; i++) extra_scratch[i] |= mask;
            }
            for (int i = lo_all; i < hi_all; i++) {
                if (!extra_scratch[i]) continue;
                total += ceil(constant * note_weights[i]
                              * (1.0 + combined(&ctx, fixed_bits[i] | extra_scratch[i])))
                         - base_scratch[i];
            }
        }
        if (!have || total > best) { best = total; have = 1; for (int s = 0; s < SLOTS; s++) best_perm[s] = p[s]; }
    next_order:;
        int i = SLOTS - 2;
        while (i >= 0 && p[i] > p[i + 1]) i--;
        if (i < 0) break;
        int j = SLOTS - 1;
        while (p[j] < p[i]) j--;
        int t = p[i]; p[i] = p[j]; p[j] = t;
        for (int a = i + 1, b = SLOTS - 1; a < b; a++, b--) { t = p[a]; p[a] = p[b]; p[b] = t; }
    }
    return have ? best : NAN;
}

/* ------------------------------------------------------------------------------------------
 * ONE CALL PER (TEAM, CHART): the per-chart preparation of `song._score_song` and the walk.
 *
 * The team arrives as TEMPLATES, its terms with everything no chart moves (bucket, magnitude,
 * occupancy, recipients, an ACTIVE's interval and duration, a COMBO-gated override's two
 * magnitudes, each SPECIAL rider's boosted occupancy on it), built once per board in Python
 * (`song._chart_kernel_spec`). Here, on one chart, as the Python does it and in its order:
 *   - each ACTIVE's windows on its cooldown lattice (`song._lattice_windows`), split by the
 *     chart's combo at each check when its override is combo-gated (`song._combo_split`);
 *   - the SPECIAL activation-rate riders on the windows their host's slot covers
 *     (`song._rider_hosts`, `song._rider_terms`), the leader's outfit terms last;
 *   - the live masks, the SPECIAL cover ranges, the coupling test, and the walk itself
 *     (`hd_walk` / `hd_walk_general`).
 * Returns 0 with the total and the winning order; 1 when the team needs the Python walk (more
 * terms than the masks hold, two riders that can share a check, more windows than this holds);
 * 2 when `allowed` admits no order. Bit-identical to the Python (`test_notewalk.py`).
 */

#define CK_TI 11      /* ints per template */
#define CK_TD 6       /* doubles per template */
#define CK_HI 3       /* ints per rider host */
#define CK_MAX_WIN 2048

enum { K_ALWAYS = 0, K_NEVER = 1, K_ACTIVE = 2, K_SPECIAL = 3, K_RIDER = 4 };

typedef struct {
    int kind;                 /* K_ALWAYS, K_NEVER (no window), K_ACTIVE (windows), K_SPECIAL, K_RIDER */
    int tmpl;                 /* its template: bucket, recipients, rider occupancies */
    int member;               /* a SPECIAL's member, a rider's host */
    int w0, wn;               /* windows [w0, w0 + wn) of the window buffer */
    int rider_base;           /* a rider's ACTIVE term (index among the member terms) */
    double sdur;              /* a SPECIAL's duration */
    double magnitude, occupancy;
    int cov_ok[SLOTS];        /* a rider: live when its host sits in this slot */
    double cov_s[SLOTS], cov_e[SLOTS];
} Prep;

/* Python's bisect.bisect_left on an ascending double array */
static int bisect_left_d(const double *a, int n, double x) {
    int lo = 0, hi = n;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (a[mid] < x) lo = mid + 1; else hi = mid;
    }
    return lo;
}

/* `song._lattice_windows`: check k at k * interval for k = 1, 2, ... while <= song_seconds */
static int lattice(double interval, double duration, double song_seconds,
                   double *ws, double *we, int at, int cap) {
    int n = 0;
    for (long k = 1; (double)k * interval <= song_seconds; k++) {
        if (at + n >= cap) return -1;
        double start = (double)k * interval;
        ws[at + n] = start;
        we[at + n] = start + duration;
        n++;
    }
    return n;
}

int hd_score_chart(int nt, const int32_t *ti, const double *td, const double *rq,
                   int nh, const int32_t *hi, const double *hd,
                   int note_count, const double *times, const double *note_weights,
                   const double *cues, double song_seconds, double constant,
                   const double *score_rates, int nrates, double skill_effect_up,
                   const uint8_t *allowed, double *base_scratch, uint64_t *extra_scratch,
                   uint64_t *bits, double *out_total, int32_t *out_perm) {
    /* on the stack: the server walks in threads with the GIL released */
    double ws[CK_MAX_WIN], we[CK_MAX_WIN];
    Prep p[MAX_TERMS];
    int np = 0, nw = 0;

    /* 1. the member terms, in recipe order, each ACTIVE split by the combo it is reached at */
    for (int t = 0; t < nt; t++) {
        const int32_t *a = ti + t * CK_TI;
        const double *d = td + t * CK_TD;
        if (!a[9]) continue; /* the outfit's terms come last */
        int kind = a[0];
        if (kind == K_ACTIVE) {
            int wn = lattice(d[2], d[3], song_seconds, ws, we, nw, CK_MAX_WIN);
            if (wn < 0) return 1;
            if (a[7] && wn > 0) {
                /* `_combo_split`: the checks the chart's combo has reached first, then the rest */
                int w0 = nw;
                double hs[256], he[256], ms[256], me[256];
                int nhit = 0, nmiss = 0;
                if (wn > 256) return 1;
                for (int w = 0; w < wn; w++) {
                    int combo = bisect_left_d(times, note_count, ws[w0 + w]);
                    int reached = a[7] == 2 ? combo <= a[8] : combo >= a[8];
                    if (reached) { hs[nhit] = ws[w0 + w]; he[nhit] = we[w0 + w]; nhit++; }
                    else { ms[nmiss] = ws[w0 + w]; me[nmiss] = we[w0 + w]; nmiss++; }
                }
                for (int w = 0; w < nhit; w++) { ws[w0 + w] = hs[w]; we[w0 + w] = he[w]; }
                for (int w = 0; w < nmiss; w++) { ws[w0 + nhit + w] = ms[w]; we[w0 + nhit + w] = me[w]; }
                if (nhit) {
                    if (np >= MAX_TERMS) return 1;
                    Prep *q = &p[np++];
                    q->kind = K_ACTIVE; q->tmpl = t; q->member = -1; q->rider_base = -1; q->sdur = 0.0;
                    q->w0 = w0; q->wn = nhit; q->magnitude = d[4]; q->occupancy = d[1];
                }
                if (nmiss) {
                    if (np >= MAX_TERMS) return 1;
                    Prep *q = &p[np++];
                    q->kind = K_ACTIVE; q->tmpl = t; q->member = -1; q->rider_base = -1; q->sdur = 0.0;
                    q->w0 = w0 + nhit; q->wn = nmiss; q->magnitude = d[5]; q->occupancy = d[1];
                }
                nw += wn;
                continue;
            }
            if (np >= MAX_TERMS) return 1;
            Prep *q = &p[np++];
            q->kind = wn > 0 ? K_ACTIVE : K_NEVER; q->tmpl = t; q->member = -1; q->rider_base = -1;
            q->sdur = 0.0; q->w0 = nw; q->wn = wn; q->magnitude = d[0]; q->occupancy = d[1];
            nw += wn;
            continue;
        }
        if (np >= MAX_TERMS) return 1;
        Prep *q = &p[np++];
        q->kind = kind; q->tmpl = t; q->member = kind == K_SPECIAL ? a[6] : -1; q->rider_base = -1;
        q->sdur = kind == K_SPECIAL ? d[3] : 0.0; q->w0 = 0; q->wn = 0;
        q->magnitude = d[0]; q->occupancy = d[1];
    }
    int nmember = np;

    /* 2. the riders: per host (its SPECIAL's span per cue, its gate read at each cue), per
     *    member term it boosts, one term per k-th window its slots cover */
    for (int h = 0; h < nh; h++) {
        const int32_t *b = hi + h * CK_HI;
        double dur = hd[h];
        int ok[SLOTS]; double ss[SLOTS], se[SLOTS];
        for (int s = 0; s < SLOTS; s++) {
            int on;
            if (b[1] == 2 || b[1] == 3) {
                int combo = bisect_left_d(times, note_count, cues[s]);
                on = b[1] == 3 ? combo <= b[2] : combo >= b[2];
            } else on = b[1] == 1;
            ok[s] = on; ss[s] = cues[s]; se[s] = cues[s] + dur;
        }
        for (int base = 0; base < nmember; base++) {
            const Prep *m = &p[base];
            double occ = rq[m->tmpl * (nh > 0 ? nh : 1) + h];
            if (m->kind != K_ACTIVE || m->wn == 0 || !(occ >= 0.0)) continue;
            int idx[SLOTS][256], cnt[SLOTS], most = 0;
            if (m->wn > 256) return 1;
            for (int s = 0; s < SLOTS; s++) {
                cnt[s] = 0;
                if (!ok[s]) continue;
                for (int w = 0; w < m->wn; w++) {
                    double st = ws[m->w0 + w];
                    if (ss[s] <= st && st < se[s]) idx[s][cnt[s]++] = m->w0 + w;
                }
                if (cnt[s] > most) most = cnt[s];
            }
            for (int k = 0; k < most; k++) {
                if (np >= MAX_TERMS) return 1;
                Prep *q = &p[np++];
                q->kind = K_RIDER; q->tmpl = m->tmpl; q->member = b[0]; q->rider_base = base;
                q->sdur = 0.0; q->w0 = 0; q->wn = 0; q->magnitude = m->magnitude; q->occupancy = occ;
                for (int s = 0; s < SLOTS; s++) {
                    q->cov_ok[s] = k < cnt[s];
                    q->cov_s[s] = k < cnt[s] ? ws[idx[s][k]] : 0.0;
                    q->cov_e[s] = k < cnt[s] ? we[idx[s][k]] : 0.0;
                }
            }
        }
    }

    /* 3. the leader's outfit terms */
    for (int t = 0; t < nt; t++) {
        const int32_t *a = ti + t * CK_TI;
        const double *d = td + t * CK_TD;
        if (a[9]) continue;
        if (np >= MAX_TERMS) return 1;
        Prep *q = &p[np++];
        q->tmpl = t; q->member = -1; q->rider_base = -1; q->sdur = 0.0; q->w0 = nw; q->wn = 0;
        q->magnitude = d[0]; q->occupancy = d[1];
        if (a[0] == K_ACTIVE) {
            int wn = lattice(d[2], d[3], song_seconds, ws, we, nw, CK_MAX_WIN);
            if (wn < 0) return 1;
            q->wn = wn; nw += wn;
            q->kind = wn > 0 ? K_ACTIVE : K_NEVER;
        } else if (a[0] == K_SPECIAL) {
            return 1; /* an outfit has no slot (the Python raises) */
        } else q->kind = a[0];
    }

    /* `song._riders_share_a_check`: two riders of one ACTIVE term, on different hosts, on the
     * same window in two different slots -- the walk below knows no `_strongest_riders` */
    for (int i = 0; i < np; i++) {
        if (p[i].kind != K_RIDER) continue;
        for (int j = i + 1; j < np; j++) {
            if (p[j].kind != K_RIDER || p[j].rider_base != p[i].rider_base || p[j].member == p[i].member) continue;
            for (int sa = 0; sa < SLOTS; sa++) {
                if (!p[i].cov_ok[sa]) continue;
                for (int sb = 0; sb < SLOTS; sb++)
                    if (sb != sa && p[j].cov_ok[sb] && p[j].cov_s[sb] == p[i].cov_s[sa]
                        && p[j].cov_e[sb] == p[i].cov_e[sa])
                        return 1;
            }
        }
    }

    /* the terms as the walk reads them, the live masks and the SPECIAL cover ranges */
    Term terms[MAX_TERMS];
    uint64_t always = 0;
    int32_t special_bit[MAX_TERMS], special_member[MAX_TERMS], cover[MAX_TERMS * SLOTS * 2];
    int nspecial = 0;
    for (int i = 0; i < np; i++) {
        const int32_t *a = ti + p[i].tmpl * CK_TI;
        Term *o = &terms[i];
        o->bucket = a[1]; o->is_boost = a[2]; o->owner = a[3];
        o->has_target = a[4]; o->target_mask = (uint32_t)a[5]; o->pad_ = 0;
        o->magnitude = p[i].magnitude; o->occupancy = p[i].occupancy;
        if (p[i].kind == K_ALWAYS) always |= 1ull << i;
    }
    for (int i = 0; i < note_count; i++) bits[i] = always;
    for (int i = 0; i < np; i++) {
        if (p[i].kind == K_ACTIVE) {
            uint64_t mask = 1ull << i;
            for (int w = 0; w < p[i].wn; w++) {
                int lo = bisect_left_d(times, note_count, ws[p[i].w0 + w]);
                int hi_ = bisect_left_d(times, note_count, we[p[i].w0 + w]);
                for (int k = lo; k < hi_; k++) bits[k] |= mask;
            }
        } else if (p[i].kind == K_SPECIAL || p[i].kind == K_RIDER) {
            int32_t *c = cover + nspecial * SLOTS * 2;
            for (int s = 0; s < SLOTS; s++) {
                if (p[i].kind == K_SPECIAL) {
                    c[2 * s] = bisect_left_d(times, note_count, cues[s]);
                    c[2 * s + 1] = bisect_left_d(times, note_count, cues[s] + p[i].sdur);
                } else if (p[i].cov_ok[s]) {
                    c[2 * s] = bisect_left_d(times, note_count, p[i].cov_s[s]);
                    c[2 * s + 1] = bisect_left_d(times, note_count, p[i].cov_e[s]);
                } else { c[2 * s] = 0; c[2 * s + 1] = 0; }
            }
            special_bit[nspecial] = i;
            special_member[nspecial] = p[i].member;
            nspecial++;
        }
    }

    /* coupled: a note reachable from two different slots by two different members */
    int coupled = 0;
    for (int x = 0; x < nspecial && !coupled; x++)
        for (int sx = 0; sx < SLOTS && !coupled; sx++) {
            int lx = cover[(x * SLOTS + sx) * 2], hx = cover[(x * SLOTS + sx) * 2 + 1];
            if (lx >= hx) continue;
            for (int y = 0; y < nspecial && !coupled; y++)
                for (int sy = 0; sy < SLOTS; sy++) {
                    if (y == x && sy <= sx) continue;
                    if (y < x) continue;
                    int ly = cover[(y * SLOTS + sy) * 2], hy = cover[(y * SLOTS + sy) * 2 + 1];
                    if (ly >= hy) continue;
                    if (sx != sy && special_member[x] != special_member[y] && lx < hy && ly < hx) {
                        coupled = 1;
                        break;
                    }
                }
        }

    double total = (coupled ? hd_walk_general : hd_walk)(
        note_count, note_weights, bits, constant, terms, np, score_rates, nrates, skill_effect_up,
        nspecial, special_bit, special_member, cover, out_perm, base_scratch, extra_scratch, allowed);
    if (total != total) return 2;
    *out_total = total;
    return 0;
}
