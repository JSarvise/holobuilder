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
               uint64_t *extra_scratch) {
    /* on the stack, ~13 KB: the web server scores in THREADS with the GIL released; a
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
    for (;;) {
        double total = fixed_total;
        for (int slot = 0; slot < SLOTS; slot++) total += delta[p[slot]][slot];
        if (!have || total > best) { best = total; have = 1; for (int s = 0; s < SLOTS; s++) best_perm[s] = p[s]; }
        int i = SLOTS - 2;
        while (i >= 0 && p[i] > p[i + 1]) i--;
        if (i < 0) break;
        int j = SLOTS - 1;
        while (p[j] < p[i]) j--;
        int t = p[i]; p[i] = p[j]; p[j] = t;
        for (int a = i + 1, b = SLOTS - 1; a < b; a++, b--) { t = p[a]; p[a] = p[b]; p[b] = t; }
    }
    return best;
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
                       uint64_t *extra_scratch) {
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
    for (;;) {
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
        int i = SLOTS - 2;
        while (i >= 0 && p[i] > p[i + 1]) i--;
        if (i < 0) break;
        int j = SLOTS - 1;
        while (p[j] < p[i]) j--;
        int t = p[i]; p[i] = p[j]; p[j] = t;
        for (int a = i + 1, b = SLOTS - 1; a < b; a++, b--) { t = p[a]; p[a] = p[b]; p[b] = t; }
    }
    return best;
}
