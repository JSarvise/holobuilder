/* Cooldown variants of one team on one chart, as an ESTIMATE for ordering them
 * (`hbuilder.domain.song.screen_song_cooldown_variants`).
 *
 * A variant gives each member's activation-frequency term (the board's cooldown
 * shortening); only the ACTIVE windows move with it. The caller describes the team once:
 * the terms no cooldown moves, each ACTIVE skill's lattice (interval, duration, base
 * reduction, its combo-gated override) and the SPECIAL activation-rate riders that ride
 * its checks. For each variant this file builds the term list exactly as
 * `song.member_terms_for` + `song._rider_terms` would (the same doubles in the same
 * order for the lattice, the note ranges and the combo split), then walks the chart by
 * RUNS of notes with a constant live set instead of note by note:
 *
 *   total = SUM_run c * W_run * (1 + R(mask_run)) + 0.5 * notes_run
 *         + max over the 120 slot orders of the slot-dependent terms' extra
 *
 * with W_run from a prefix sum of the note weights and R the exact pool (`hd_pool`,
 * notewalk.c). The per-note `ceil` is replaced by its mean (+0.5 a note), so a total is
 * within a few 1e-5 of `score_song_cooldown_variants`'s: an order, never a served number.
 * The slot orders: per-(member, slot) deltas of the slot-dependent terms (SPECIALs and
 * riders), plus a correction for each pair of (member, slot) choices whose note ranges
 * overlap (three-way overlaps are left out).
 *
 * ONE LAYOUT FOR EVERY VARIANT. A variant moves windows, never a term's values, so
 * `hd_cdvar` numbers the terms once for all of them (each ACTIVE part and each rider copy a
 * bit of its own, present or not in a given variant) and keeps ONE memo of the pool R(mask)
 * across the variants: a mask the screen met in one combination of counts is never computed
 * again in another. A variant's live terms keep their relative order, so every R is the
 * same double as in the per-variant layout (`hd_cdvar_each`, the reference it is held to,
 * bit for bit); when the shared layout needs more than MAX_TERMS bits it is that layout.
 *
 * Built with cbuild.py's flags like the other kernels. Returns NAN for a variant with
 * more than MAX_TERMS terms (the caller scores it exactly).
 */
#include <stdlib.h>
/* one memo for the variants of a call, emptied when half full (a cache: the values never
 * depend on it); 8192 slots keep a call's scratch under the size the allocator hands out
 * from its own pages */
#define MEMO_BITS 13
#include "notewalk.c"

#define MAX_SEG 4096
#define MAX_RANGES 4096
#define MAX_WIN 512

typedef struct {
    int nseg;
    int32_t start[MAX_SEG + 1];
    uint64_t mask[MAX_SEG];
    double r[MAX_SEG]; /* R(mask) of each segment, for every delta */
} Segs;

#define MAX_NOTES 8192 /* a chart past this is declined (the caller scores it exactly) */

/* One call's scratch, allocated per call: the web server scores in threads with the GIL
 * released, so nothing here may be static. */
typedef struct {
    Ctx ctx;
    Segs sg;
    /* per note index: whether some window starts or ends there, and the bits it flips */
    uint8_t mark[MAX_NOTES + 1];
    uint64_t flip[MAX_NOTES + 1];
    int32_t rng[3 * MAX_RANGES];
    double win_start[MAX_WIN], win_end[MAX_WIN];
    int32_t slo[SLOTS][SLOTS][MAX_TERMS], shi[SLOTS][SLOTS][MAX_TERMS];
    uint64_t sbit[SLOTS][SLOTS][MAX_TERMS];
    int sn[SLOTS][SLOTS];
    double pairc[SLOTS][SLOTS][SLOTS][SLOTS];
} Work;

/* bisect_left over ascending doubles */
static int32_t lower_bound(const double *times, int n, double x) {
    int lo = 0, hi = n;
    while (lo < hi) {
        int mid = (lo + hi) >> 1;
        if (times[mid] < x) lo = mid + 1; else hi = mid;
    }
    return lo;
}

/* SUM over the pieces of the spans' union of c * W * (R(run | extra) - R(run)) */
static double extra_delta(Ctx *ctx, const Segs *sg, const double *prefix, double constant,
                          int nspan, const int32_t *lo, const int32_t *hi, const uint64_t *bits) {
    int32_t pts[4 * MAX_TERMS];
    int np = 0;
    for (int i = 0; i < nspan; i++) { pts[np++] = lo[i]; pts[np++] = hi[i]; }
    for (int i = 1; i < np; i++) { int32_t k = pts[i]; int j = i - 1; while (j >= 0 && pts[j] > k) { pts[j + 1] = pts[j]; j--; } pts[j + 1] = k; }
    double change = 0.0;
    int k = -1;
    for (int a = 0; a + 1 < np; a++) {
        int32_t u = pts[a], v = pts[a + 1];
        if (u >= v) continue;
        uint64_t extra = 0;
        for (int i = 0; i < nspan; i++) if (lo[i] <= u && v <= hi[i]) extra |= bits[i];
        if (!extra) continue;
        if (k < 0) {  /* the first piece: the last segment starting at or before u, by bisection */
            int l = 0, h = sg->nseg;
            while (l < h) { int m = (l + h) >> 1; if (sg->start[m] <= u) l = m + 1; else h = m; }
            k = l > 0 ? l - 1 : 0;
        }
        while (k + 1 < sg->nseg && sg->start[k + 1] <= u) k++;
        int32_t x = u;
        int kk = k;
        while (x < v && kk < sg->nseg) {
            int32_t y = sg->start[kk + 1] < v ? sg->start[kk + 1] : v;
            if (y > x) {
                double w = prefix[y] - prefix[x];
                change += constant * w * (combined(ctx, sg->mask[kk] | extra) - sg->r[kk]);
            }
            x = y;
            kk++;
        }
    }
    return change;
}

/* The walk of one variant's terms. `rng` = (bit, lo, hi) windows; slot-dependent terms
 * (`nslot`): bit, member, cover [SLOTS][2]. */
static double walk_runs(Work *wk, int note_count, const double *prefix, double constant,
                        double bias, uint64_t always_mask, int nrng, const int32_t *rng,
                        int nslot, const int32_t *slot_bit, const int32_t *slot_member,
                        const int32_t *slot_cover, const uint8_t *allowed) {
    Ctx *ctx = &wk->ctx;
    Segs *sg = &wk->sg;
    /* the runs: a segment boundary at every note index some window starts or ends at (also
     * where two flips cancel), the live set flipped there by those windows' bits */
    uint8_t *mark = wk->mark;
    uint64_t *flip = wk->flip;
    memset(mark, 0, (size_t)note_count + 1);
    for (int i = 0; i < nrng; i++) {
        int32_t lo = rng[3 * i + 1], hi = rng[3 * i + 2];
        if (lo < 0) lo = 0;
        if (hi > note_count) hi = note_count;
        if (lo >= hi) continue;
        uint64_t bit = 1ull << rng[3 * i];
        if (!mark[lo]) { mark[lo] = 1; flip[lo] = 0; }
        flip[lo] ^= bit;
        if (!mark[hi]) { mark[hi] = 1; flip[hi] = 0; }
        flip[hi] ^= bit;
    }
    uint64_t cur = always_mask;
    int ns = 0;
    int32_t at = 0;
    if (mark[0]) cur ^= flip[0];
    for (;;) {
        int32_t next = at + 1;
        while (next < note_count && !mark[next]) next++;
        if (next > note_count) next = note_count;
        if (next > at && ns < MAX_SEG) { sg->start[ns] = at; sg->mask[ns] = cur; ns++; }
        if (next >= note_count) break;
        at = next;
        cur ^= flip[at];
    }
    sg->nseg = ns;
    sg->start[ns] = note_count;
    double fixed = 0.0;
    for (int s = 0; s < ns; s++) {
        int32_t a = sg->start[s], b = sg->start[s + 1];
        double r = sg->r[s] = combined(ctx, sg->mask[s]);
        fixed += constant * (prefix[b] - prefix[a]) * (1.0 + r) + bias * (double)(b - a);
    }
    /* per (owner, slot) spans, deltas, pair corrections */
    int32_t (*slo)[SLOTS][MAX_TERMS] = wk->slo, (*shi)[SLOTS][MAX_TERMS] = wk->shi;
    uint64_t (*sbit)[SLOTS][MAX_TERMS] = wk->sbit;
    int (*sn)[SLOTS] = wk->sn;
    double (*pairc)[SLOTS][SLOTS][SLOTS] = wk->pairc;
    double delta[SLOTS][SLOTS];
    memset(delta, 0, sizeof delta);
    memset(wk->sn, 0, sizeof wk->sn);
    memset(wk->pairc, 0, sizeof wk->pairc);
    for (int k = 0; k < nslot; k++) {
        int o = slot_member[k];
        if (o < 0 || o >= SLOTS) continue;
        for (int slot = 0; slot < SLOTS; slot++) {
            int32_t a = slot_cover[(k * SLOTS + slot) * 2], b = slot_cover[(k * SLOTS + slot) * 2 + 1];
            if (a < 0) a = 0;
            if (b > note_count) b = note_count;
            if (a < b) { int n = sn[o][slot]++; slo[o][slot][n] = a; shi[o][slot][n] = b; sbit[o][slot][n] = 1ull << slot_bit[k]; }
        }
    }
    for (int o = 0; o < SLOTS; o++)
        for (int slot = 0; slot < SLOTS; slot++)
            if (sn[o][slot]) delta[o][slot] = extra_delta(ctx, sg, prefix, constant, sn[o][slot], slo[o][slot], shi[o][slot], sbit[o][slot]);
    int32_t lo[2 * MAX_TERMS], hi[2 * MAX_TERMS];
    uint64_t bits[2 * MAX_TERMS];
    for (int o1 = 0; o1 < SLOTS; o1++) for (int o2 = o1 + 1; o2 < SLOTS; o2++)
        for (int s1 = 0; s1 < SLOTS; s1++) for (int s2 = 0; s2 < SLOTS; s2++) {
            if (s1 == s2 || !sn[o1][s1] || !sn[o2][s2]) continue;
            int ov = 0;
            for (int i = 0; i < sn[o1][s1] && !ov; i++)
                for (int j = 0; j < sn[o2][s2]; j++)
                    if (slo[o1][s1][i] < shi[o2][s2][j] && slo[o2][s2][j] < shi[o1][s1][i]) { ov = 1; break; }
            if (!ov) continue;
            int n = 0;
            for (int i = 0; i < sn[o1][s1]; i++) { lo[n] = slo[o1][s1][i]; hi[n] = shi[o1][s1][i]; bits[n] = sbit[o1][s1][i]; n++; }
            for (int j = 0; j < sn[o2][s2]; j++) { lo[n] = slo[o2][s2][j]; hi[n] = shi[o2][s2][j]; bits[n] = sbit[o2][s2][j]; n++; }
            double both = extra_delta(ctx, sg, prefix, constant, n, lo, hi, bits);
            double c = both - delta[o1][s1] - delta[o2][s2];
            pairc[o1][s1][o2][s2] = c;
            pairc[o2][s2][o1][s1] = c;
        }
    int p[SLOTS] = {0, 1, 2, 3, 4};
    double best = 0.0; int have = 0;
    /* `allowed` (NULL: every order): one outcome of a recipients tie, as in notewalk.c */
    for (int index = 0;; index++) {
        if (!allowed || allowed[index]) {
            double total = fixed;
            for (int s = 0; s < SLOTS; s++) total += delta[p[s]][s];
            for (int s = 0; s < SLOTS; s++) for (int t = s + 1; t < SLOTS; t++) total += pairc[p[s]][s][p[t]][t];
            if (!have || total > best) { best = total; have = 1; }
        }
        int i = SLOTS - 2; while (i >= 0 && p[i] > p[i + 1]) i--; if (i < 0) break;
        int j = SLOTS - 1; while (p[j] < p[i]) j--;
        int t = p[i]; p[i] = p[j]; p[j] = t;
        for (int a = i + 1, b = SLOTS - 1; a < b; a++, b--) { t = p[a]; p[a] = p[b]; p[b] = t; }
    }
    return have ? best : NAN;
}

/* Field layouts of the flat arrays the binding (cdvar.py) packs:
 *   fixed   ints FI = 17: bucket, is_boost, owner, has_target, target_mask, live (0 always,
 *                         1 never, 2 slot-dependent), member, cover[10]
 *           dbls 2: magnitude, occupancy
 *   active  ints AI = 10: bucket, is_boost, owner, has_target, target_mask, member, combo
 *                         (0 none, 1 >=, 2 <=), count, rider_first, rider_count
 *           dbls AD = 7: occupancy, interval, duration, red_base, mag_single, mag_hit, mag_miss
 *   riders  ints: host index; dbls: q
 *   hosts   ints HI = 6: member, present[5]; dbls 10: span[5][2]
 *   order   ints pairs (kind 0 fixed / 1 active, index); then `ncostume` fixed indices
 *   cd      dbls [nvar][SLOTS]: each member's cooldown shortening in that variant
 */
#define FI 17
#define AI 10
#define AD 7
#define HI 6

/* The per-variant layout: each variant's terms numbered as `song.member_terms_for` +
 * `song._rider_terms` emit them, the memo emptied between variants. The reference
 * `hd_cdvar` is held to, and its layout when the shared one does not fit. */
double hd_cdvar_each(int note_count, const double *times, const double *prefix, double song_seconds,
                     double constant, double bias, const double *score_rates, int nrates, double skill_effect_up,
                     int nfixed, const int32_t *fixed_i, const double *fixed_d,
                     int nactive, const int32_t *active_i, const double *active_d,
                     const int32_t *rider_host, const double *rider_q,
                     int nhost, const int32_t *host_i, const double *host_d,
                     int norder, const int32_t *order, int ncostume, const int32_t *costume,
                     int nvar, const double *cd, double *out, const uint8_t *allowed) {
    if (note_count > MAX_NOTES) { for (int v = 0; v < nvar; v++) out[v] = NAN; return 1.0; }
    Work *wk = (Work *)malloc(sizeof(Work));
    if (wk == NULL) { for (int v = 0; v < nvar; v++) out[v] = NAN; return 1.0; }
    int32_t *rng = wk->rng;
    double *win_start = wk->win_start, *win_end = wk->win_end;
    Term terms[MAX_TERMS];
    int32_t slot_bit[MAX_TERMS], slot_member[MAX_TERMS], slot_cover[MAX_TERMS * SLOTS * 2];
    /* each emitted ACTIVE part: its term index, recipe, windows [first, first + n) */
    int32_t part_term[MAX_TERMS], part_recipe[MAX_TERMS], part_first[MAX_TERMS], part_n[MAX_TERMS];
    Ctx *ctxp = &wk->ctx;
    int ok_all = 1;
    for (int v = 0; v < nvar; v++) {
        int nt = 0, nr = 0, nslot = 0, nparts = 0, nwin = 0, overflow = 0;
        uint64_t always_mask = 0;
#define ADD_TERM(bucket_, boost_, owner_, has_t_, mask_, mag_, occ_) \
        do { if (nt >= MAX_TERMS) { overflow = 1; break; } \
             terms[nt].bucket = (bucket_); terms[nt].is_boost = (boost_); terms[nt].owner = (owner_); \
             terms[nt].has_target = (has_t_); terms[nt].target_mask = (uint32_t)(mask_); terms[nt].pad_ = 0; \
             terms[nt].magnitude = (mag_); terms[nt].occupancy = (occ_); nt++; } while (0)
        for (int o = 0; o < norder && !overflow; o++) {
            int kind = order[2 * o], idx = order[2 * o + 1];
            if (kind == 0) {
                const int32_t *fi = fixed_i + FI * idx;
                const double *fd = fixed_d + 2 * idx;
                if (fi[5] == 1) continue;
                ADD_TERM(fi[0], fi[1], fi[2], fi[3], fi[4], fd[0], fd[1]);
                if (overflow) break;
                if (fi[5] == 0) always_mask |= 1ull << (nt - 1);
                else {
                    slot_bit[nslot] = nt - 1; slot_member[nslot] = fi[6];
                    for (int q = 0; q < 2 * SLOTS; q++) slot_cover[nslot * 2 * SLOTS + q] = fi[7 + q];
                    nslot++;
                }
                continue;
            }
            const int32_t *ai = active_i + AI * idx;
            const double *ad = active_d + AD * idx;
            int member = ai[5];
            double reduction = ad[3] + cd[v * SLOTS + member];
            double interval = ad[1] / (1.0 + reduction);
            double duration = ad[2];
            int first = nwin;
            for (int k = 1; ; k++) {
                double start = k * interval;
                if (!(start <= song_seconds)) break;
                if (nwin >= MAX_WIN) { overflow = 1; break; }
                win_start[nwin] = start; win_end[nwin] = start + duration; nwin++;
            }
            if (overflow) break;
            int count = nwin - first;
            if (count == 0) continue;
            /* the combo split: the override on the checks the chart's combo has reached */
            int split = ai[6] != 0;
            for (int part = 0; part < (split ? 2 : 1) && !overflow; part++) {
                double mag = split ? (part == 0 ? ad[5] : ad[6]) : ad[4];
                int pfirst = nwin, pn = 0;
                if (split) {
                    for (int w = first; w < first + count; w++) {
                        int32_t combo = lower_bound(times, note_count, win_start[w]);
                        int reached = ai[6] == 2 ? combo <= ai[7] : combo >= ai[7];
                        if (reached == (part == 0)) {
                            if (nwin >= MAX_WIN) { overflow = 1; break; }
                            win_start[nwin] = win_start[w]; win_end[nwin] = win_end[w]; nwin++; pn++;
                        }
                    }
                    if (overflow || pn == 0) continue;
                } else {
                    pfirst = first; pn = count;
                }
                ADD_TERM(ai[0], ai[1], ai[2], ai[3], ai[4], mag, ad[0]);
                if (overflow) break;
                int bit = nt - 1;
                for (int w = pfirst; w < pfirst + pn; w++) {
                    if (nr >= MAX_RANGES) { overflow = 1; break; }
                    rng[3 * nr] = bit;
                    rng[3 * nr + 1] = lower_bound(times, note_count, win_start[w]);
                    rng[3 * nr + 2] = lower_bound(times, note_count, win_end[w]);
                    nr++;
                }
                part_term[nparts] = bit; part_recipe[nparts] = idx;
                part_first[nparts] = pfirst; part_n[nparts] = pn; nparts++;
            }
        }
        /* the riders: per host, per ACTIVE part (in order), one term per k-th covered window */
        for (int h = 0; h < nhost && !overflow; h++) {
            const int32_t *hi_ = host_i + HI * h;
            const double *hd_ = host_d + 2 * SLOTS * h;
            for (int pp = 0; pp < nparts && !overflow; pp++) {
                const int32_t *ai = active_i + AI * part_recipe[pp];
                double q = -1.0;
                for (int r = 0; r < ai[9]; r++)
                    if (rider_host[ai[8] + r] == h) { q = rider_q[ai[8] + r]; break; }
                if (q < 0.0) continue;
                int per_slot_n[SLOTS];
                int per_slot_w[SLOTS][MAX_WIN > 64 ? 64 : MAX_WIN];
                int kmax = 0;
                for (int s = 0; s < SLOTS; s++) {
                    per_slot_n[s] = 0;
                    if (!hi_[1 + s]) continue;
                    double lo = hd_[2 * s], hi = hd_[2 * s + 1];
                    for (int w = part_first[pp]; w < part_first[pp] + part_n[pp]; w++)
                        if (lo <= win_start[w] && win_start[w] < hi && per_slot_n[s] < 64)
                            per_slot_w[s][per_slot_n[s]++] = w;
                    if (per_slot_n[s] > kmax) kmax = per_slot_n[s];
                }
                const Term *t = &terms[part_term[pp]];
                for (int k = 0; k < kmax && !overflow; k++) {
                    ADD_TERM(t->bucket, t->is_boost, t->owner, t->has_target, t->target_mask, t->magnitude, q);
                    if (overflow) break;
                    slot_bit[nslot] = nt - 1; slot_member[nslot] = hi_[0];
                    for (int s = 0; s < SLOTS; s++) {
                        int32_t a = 0, b = 0;
                        if (k < per_slot_n[s]) {
                            int w = per_slot_w[s][k];
                            a = lower_bound(times, note_count, win_start[w]);
                            b = lower_bound(times, note_count, win_end[w]);
                        }
                        slot_cover[(nslot * SLOTS + s) * 2] = a;
                        slot_cover[(nslot * SLOTS + s) * 2 + 1] = b;
                    }
                    nslot++;
                }
            }
        }
        for (int c = 0; c < ncostume && !overflow; c++) {
            const int32_t *fi = fixed_i + FI * costume[c];
            const double *fd = fixed_d + 2 * costume[c];
            if (fi[5] == 1) continue;
            ADD_TERM(fi[0], fi[1], fi[2], fi[3], fi[4], fd[0], fd[1]);
            if (overflow) break;
            if (fi[5] == 0) always_mask |= 1ull << (nt - 1);
            else {
                slot_bit[nslot] = nt - 1; slot_member[nslot] = fi[6];
                for (int q = 0; q < 2 * SLOTS; q++) slot_cover[nslot * 2 * SLOTS + q] = fi[7 + q];
                nslot++;
            }
        }
#undef ADD_TERM
        if (overflow) { out[v] = NAN; ok_all = 0; continue; }
        ctxp->terms = terms; ctxp->nterms = nt;
        ctxp->score_rates = score_rates; ctxp->nrates = nrates;
        ctxp->skill_effect_up = skill_effect_up;
        memset(ctxp->full, 0, sizeof ctxp->full);
        ctxp->used = 0;
        out[v] = walk_runs(wk, note_count, prefix, constant, bias, always_mask, nr, rng,
                           nslot, slot_bit, slot_member, slot_cover, allowed);
    }
    free(wk);
    return ok_all ? 0.0 : 1.0;
}

/* ------------------------------------------------------------------ one layout, one memo */

#define MAX_PARTS 128 /* ACTIVE parts of a team (a combo split makes two of one skill) */
#define MAX_HOSTS 32  /* SPECIAL activation-rate rider hosts */
#define RIDER_CAP 64  /* windows of one part a host's span counts, as in hd_cdvar_each */

/* What every variant of a call shares: the static ACTIVE parts, the rider rates, the
 * numbering and the terms. */
typedef struct {
    int nsp;
    int32_t sp_recipe[MAX_PARTS], sp_bit[MAX_PARTS];
    double sp_mag[MAX_PARTS];
    double q[MAX_HOSTS][MAX_PARTS];      /* rider rate of host h on part p, < 0: none */
    int32_t kmax[MAX_HOSTS][MAX_PARTS];  /* most rider copies any variant needs */
    int32_t base[MAX_HOSTS][MAX_PARTS];  /* first bit of those copies */
    int32_t pfirst[MAX_PARTS], pn[MAX_PARTS];
    Term terms[MAX_TERMS];
    uint64_t always;
    /* the slot-dependent fixed terms, before the riders (`order`) and after them (outfit) */
    int nfs, ncs;
    int32_t fs_bit[MAX_TERMS], fs_member[MAX_TERMS], fs_cover[MAX_TERMS * SLOTS * 2];
    int32_t cs_bit[MAX_TERMS], cs_member[MAX_TERMS], cs_cover[MAX_TERMS * SLOTS * 2];
} Layout;

/* One variant's ACTIVE windows, allocated in hd_cdvar_each's order (each lattice, then its
 * combo split's copies) so a MAX_WIN overflow falls on the same variants. Static part p gets
 * [pfirst[p], pfirst[p] + pn[p]); pn 0 for a part that variant does not emit. 0 on overflow. */
static int variant_windows(Work *wk, Layout *ly, int note_count, const double *times, double song_seconds,
                           int norder, const int32_t *order, const int32_t *active_i, const double *active_d,
                           const double *cdv) {
    double *win_start = wk->win_start, *win_end = wk->win_end;
    int nwin = 0, p = 0;
    for (int o = 0; o < norder; o++) {
        if (order[2 * o] != 1) continue;
        int idx = order[2 * o + 1];
        const int32_t *ai = active_i + AI * idx;
        const double *ad = active_d + AD * idx;
        int split = ai[6] != 0;
        double reduction = ad[3] + cdv[ai[5]];
        double interval = ad[1] / (1.0 + reduction);
        double duration = ad[2];
        int first = nwin;
        for (int k = 1; ; k++) {
            double start = k * interval;
            if (!(start <= song_seconds)) break;
            if (nwin >= MAX_WIN) return 0;
            win_start[nwin] = start; win_end[nwin] = start + duration; nwin++;
        }
        int count = nwin - first;
        for (int part = 0; part < (split ? 2 : 1); part++, p++) {
            ly->pfirst[p] = nwin; ly->pn[p] = 0;
            if (count == 0) continue;
            if (!split) { ly->pfirst[p] = first; ly->pn[p] = count; continue; }
            for (int w = first; w < first + count; w++) {
                int32_t combo = lower_bound(times, note_count, win_start[w]);
                int reached = ai[6] == 2 ? combo <= ai[7] : combo >= ai[7];
                if (reached == (part == 0)) {
                    if (nwin >= MAX_WIN) return 0;
                    win_start[nwin] = win_start[w]; win_end[nwin] = win_end[w]; nwin++; ly->pn[p]++;
                }
            }
        }
    }
    return 1;
}

/* The rider copies host `hi_` needs on part p in the current windows, and per slot the
 * windows they cover (`w[s][k]`, `n[s]`). */
static int rider_windows(const Work *wk, const Layout *ly, const int32_t *hi_, const double *hd_, int p,
                         int n[SLOTS], int w[SLOTS][RIDER_CAP]) {
    int kmax = 0;
    for (int s = 0; s < SLOTS; s++) {
        n[s] = 0;
        if (!hi_[1 + s]) continue;
        double lo = hd_[2 * s], hi = hd_[2 * s + 1];
        for (int x = ly->pfirst[p]; x < ly->pfirst[p] + ly->pn[p]; x++)
            if (lo <= wk->win_start[x] && wk->win_start[x] < hi && n[s] < RIDER_CAP) w[s][n[s]++] = x;
        if (n[s] > kmax) kmax = n[s];
    }
    return kmax;
}

static void put_fixed(Term *t, const int32_t *fi, const double *fd) {
    t->bucket = fi[0]; t->is_boost = fi[1]; t->owner = fi[2]; t->has_target = fi[3];
    t->target_mask = (uint32_t)fi[4]; t->pad_ = 0; t->magnitude = fd[0]; t->occupancy = fd[1];
}

/* The numbering shared by every variant, in hd_cdvar_each's order: `order` (fixed terms,
 * ACTIVE parts), the riders by host and part (as many copies as any variant needs), the
 * outfit's terms. 0 when it needs more than MAX_TERMS bits. */
static int shared_layout(Work *wk, Layout *ly, int note_count, const double *times, double song_seconds,
                         const int32_t *fixed_i, const double *fixed_d,
                         const int32_t *active_i, const double *active_d,
                         const int32_t *rider_host, const double *rider_q,
                         int nhost, const int32_t *host_i, const double *host_d,
                         int norder, const int32_t *order, int ncostume, const int32_t *costume,
                         int nvar, const double *cd) {
    int nsp = 0;
    for (int o = 0; o < norder; o++) {
        if (order[2 * o] != 1) continue;
        const int32_t *ai = active_i + AI * order[2 * o + 1];
        const double *ad = active_d + AD * order[2 * o + 1];
        int split = ai[6] != 0;
        for (int part = 0; part < (split ? 2 : 1); part++) {
            if (nsp >= MAX_PARTS) return 0;
            ly->sp_recipe[nsp] = order[2 * o + 1];
            ly->sp_mag[nsp] = split ? (part == 0 ? ad[5] : ad[6]) : ad[4];
            nsp++;
        }
    }
    ly->nsp = nsp;
    for (int h = 0; h < nhost; h++)
        for (int p = 0; p < nsp; p++) {
            const int32_t *ai = active_i + AI * ly->sp_recipe[p];
            double q = -1.0;
            for (int r = 0; r < ai[9]; r++)
                if (rider_host[ai[8] + r] == h) { q = rider_q[ai[8] + r]; break; }
            ly->q[h][p] = q;
            ly->kmax[h][p] = 0;
        }
    int nw[SLOTS], ww[SLOTS][RIDER_CAP];
    for (int v = 0; v < nvar; v++) {
        if (!variant_windows(wk, ly, note_count, times, song_seconds, norder, order, active_i, active_d, cd + v * SLOTS))
            continue;  /* a NAN variant in either layout */
        for (int h = 0; h < nhost; h++)
            for (int p = 0; p < nsp; p++) {
                if (ly->q[h][p] < 0.0 || ly->pn[p] == 0) continue;
                int k = rider_windows(wk, ly, host_i + HI * h, host_d + 2 * SLOTS * h, p, nw, ww);
                if (k > ly->kmax[h][p]) ly->kmax[h][p] = k;
            }
    }
    int nt = 0, p = 0;
    ly->always = 0; ly->nfs = ly->ncs = 0;
    for (int o = 0; o < norder; o++) {
        int kind = order[2 * o], idx = order[2 * o + 1];
        if (kind == 0) {
            const int32_t *fi = fixed_i + FI * idx;
            if (fi[5] == 1) continue;
            if (nt >= MAX_TERMS) return 0;
            put_fixed(&ly->terms[nt], fi, fixed_d + 2 * idx);
            if (fi[5] == 0) ly->always |= 1ull << nt;
            else {
                ly->fs_bit[ly->nfs] = nt; ly->fs_member[ly->nfs] = fi[6];
                for (int c = 0; c < 2 * SLOTS; c++) ly->fs_cover[ly->nfs * 2 * SLOTS + c] = fi[7 + c];
                ly->nfs++;
            }
            nt++;
            continue;
        }
        const int32_t *ai = active_i + AI * idx;
        const double *ad = active_d + AD * idx;
        for (int part = 0; part < (ai[6] != 0 ? 2 : 1); part++, p++) {
            if (nt >= MAX_TERMS) return 0;
            Term *t = &ly->terms[nt];
            t->bucket = ai[0]; t->is_boost = ai[1]; t->owner = ai[2]; t->has_target = ai[3];
            t->target_mask = (uint32_t)ai[4]; t->pad_ = 0; t->magnitude = ly->sp_mag[p]; t->occupancy = ad[0];
            ly->sp_bit[p] = nt++;
        }
    }
    for (int h = 0; h < nhost; h++)
        for (int q = 0; q < nsp; q++) {
            ly->base[h][q] = nt;
            for (int k = 0; k < ly->kmax[h][q]; k++) {
                if (nt >= MAX_TERMS) return 0;
                ly->terms[nt] = ly->terms[ly->sp_bit[q]];
                ly->terms[nt].occupancy = ly->q[h][q];
                nt++;
            }
        }
    for (int c = 0; c < ncostume; c++) {
        const int32_t *fi = fixed_i + FI * costume[c];
        if (fi[5] == 1) continue;
        if (nt >= MAX_TERMS) return 0;
        put_fixed(&ly->terms[nt], fi, fixed_d + 2 * costume[c]);
        if (fi[5] == 0) ly->always |= 1ull << nt;
        else {
            ly->cs_bit[ly->ncs] = nt; ly->cs_member[ly->ncs] = fi[6];
            for (int x = 0; x < 2 * SLOTS; x++) ly->cs_cover[ly->ncs * 2 * SLOTS + x] = fi[7 + x];
            ly->ncs++;
        }
        nt++;
    }
    return nt;
}

double hd_cdvar(int note_count, const double *times, const double *prefix, double song_seconds,
                double constant, double bias, const double *score_rates, int nrates, double skill_effect_up,
                int nfixed, const int32_t *fixed_i, const double *fixed_d,
                int nactive, const int32_t *active_i, const double *active_d,
                const int32_t *rider_host, const double *rider_q,
                int nhost, const int32_t *host_i, const double *host_d,
                int norder, const int32_t *order, int ncostume, const int32_t *costume,
                int nvar, const double *cd, double *out, const uint8_t *allowed) {
    if (note_count > MAX_NOTES) { for (int v = 0; v < nvar; v++) out[v] = NAN; return 1.0; }
    Work *wk = nhost <= MAX_HOSTS ? (Work *)malloc(sizeof(Work)) : NULL;
    Layout *ly = wk != NULL ? (Layout *)malloc(sizeof(Layout)) : NULL;
    int nt = ly == NULL ? 0
        : shared_layout(wk, ly, note_count, times, song_seconds, fixed_i, fixed_d, active_i, active_d,
                        rider_host, rider_q, nhost, host_i, host_d, norder, order, ncostume, costume, nvar, cd);
    if (nt == 0) {  /* no room, or no term at all: the per-variant layout */
        free(ly);
        free(wk);
        return hd_cdvar_each(note_count, times, prefix, song_seconds, constant, bias, score_rates, nrates,
                             skill_effect_up, nfixed, fixed_i, fixed_d, nactive, active_i, active_d, rider_host,
                             rider_q, nhost, host_i, host_d, norder, order, ncostume, costume, nvar, cd, out, allowed);
    }
    Ctx *ctxp = &wk->ctx;
    ctxp->terms = ly->terms; ctxp->nterms = nt;
    ctxp->score_rates = score_rates; ctxp->nrates = nrates;
    ctxp->skill_effect_up = skill_effect_up;
    memset(ctxp->full, 0, sizeof ctxp->full);  /* once: the memo holds for every variant */
    ctxp->used = 0;
    int32_t *rng = wk->rng;
    int32_t slot_bit[MAX_TERMS], slot_member[MAX_TERMS], slot_cover[MAX_TERMS * SLOTS * 2];
    int nw[SLOTS], ww[SLOTS][RIDER_CAP];
    int ok_all = 1;
    for (int v = 0; v < nvar; v++) {
        if (!variant_windows(wk, ly, note_count, times, song_seconds, norder, order, active_i, active_d, cd + v * SLOTS)) {
            out[v] = NAN; ok_all = 0; continue;
        }
        if (ctxp->used >= MEMO / 2) {  /* full enough to stop caching: start it again */
            memset(ctxp->full, 0, sizeof ctxp->full);
            ctxp->used = 0;
        }
        int nr = 0, overflow = 0;
        for (int q = 0; q < ly->nsp && !overflow; q++)
            for (int x = ly->pfirst[q]; x < ly->pfirst[q] + ly->pn[q]; x++) {
                if (nr >= MAX_RANGES) { overflow = 1; break; }
                rng[3 * nr] = ly->sp_bit[q];
                rng[3 * nr + 1] = lower_bound(times, note_count, wk->win_start[x]);
                rng[3 * nr + 2] = lower_bound(times, note_count, wk->win_end[x]);
                nr++;
            }
        if (overflow) { out[v] = NAN; ok_all = 0; continue; }
        int nslot = 0;
        for (int f = 0; f < ly->nfs; f++, nslot++) {
            slot_bit[nslot] = ly->fs_bit[f]; slot_member[nslot] = ly->fs_member[f];
            memcpy(slot_cover + nslot * 2 * SLOTS, ly->fs_cover + f * 2 * SLOTS, 2 * SLOTS * sizeof(int32_t));
        }
        for (int h = 0; h < nhost; h++) {
            const int32_t *hi_ = host_i + HI * h;
            const double *hd_ = host_d + 2 * SLOTS * h;
            for (int q = 0; q < ly->nsp; q++) {
                if (ly->q[h][q] < 0.0 || ly->pn[q] == 0) continue;
                int kmax = rider_windows(wk, ly, hi_, hd_, q, nw, ww);
                for (int k = 0; k < kmax; k++, nslot++) {
                    slot_bit[nslot] = ly->base[h][q] + k; slot_member[nslot] = hi_[0];
                    for (int s = 0; s < SLOTS; s++) {
                        int32_t a = 0, b = 0;
                        if (k < nw[s]) {
                            a = lower_bound(times, note_count, wk->win_start[ww[s][k]]);
                            b = lower_bound(times, note_count, wk->win_end[ww[s][k]]);
                        }
                        slot_cover[(nslot * SLOTS + s) * 2] = a;
                        slot_cover[(nslot * SLOTS + s) * 2 + 1] = b;
                    }
                }
            }
        }
        for (int f = 0; f < ly->ncs; f++, nslot++) {
            slot_bit[nslot] = ly->cs_bit[f]; slot_member[nslot] = ly->cs_member[f];
            memcpy(slot_cover + nslot * 2 * SLOTS, ly->cs_cover + f * 2 * SLOTS, 2 * SLOTS * sizeof(int32_t));
        }
        out[v] = walk_runs(wk, note_count, prefix, constant, bias, ly->always, nr, rng,
                           nslot, slot_bit, slot_member, slot_cover, allowed);
    }
    free(ly);
    free(wk);
    return ok_all ? 0.0 : 1.0;
}
