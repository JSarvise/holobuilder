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
 * Built with cbuild.py's flags like the other kernels. Returns NAN for a variant with
 * more than MAX_TERMS terms (the caller scores it exactly).
 */
#include <stdlib.h>
#include "notewalk.c"

#define MAX_SEG 4096
#define MAX_RANGES 4096
#define MAX_WIN 512

typedef struct {
    int nseg;
    int32_t start[MAX_SEG + 1];
    uint64_t mask[MAX_SEG];
} Segs;

typedef struct { int32_t pos; int32_t bit; } Ev;

/* One call's scratch, allocated per call: the web server scores in threads with the GIL
 * released, so nothing here may be static. */
typedef struct {
    Ctx ctx;
    Segs sg;
    Ev ev[2 * MAX_RANGES];
    int32_t rng[3 * MAX_RANGES];
    double win_start[MAX_WIN], win_end[MAX_WIN];
    int32_t slo[SLOTS][SLOTS][MAX_TERMS], shi[SLOTS][SLOTS][MAX_TERMS];
    uint64_t sbit[SLOTS][SLOTS][MAX_TERMS];
    int sn[SLOTS][SLOTS];
    double pairc[SLOTS][SLOTS][SLOTS][SLOTS];
} Work;

static int ev_cmp(const void *a, const void *b) {
    const Ev *x = a, *y = b;
    return (x->pos > y->pos) - (x->pos < y->pos);
}

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
    int k = 0;
    for (int a = 0; a + 1 < np; a++) {
        int32_t u = pts[a], v = pts[a + 1];
        if (u >= v) continue;
        uint64_t extra = 0;
        for (int i = 0; i < nspan; i++) if (lo[i] <= u && v <= hi[i]) extra |= bits[i];
        if (!extra) continue;
        while (k + 1 < sg->nseg && sg->start[k + 1] <= u) k++;
        int32_t x = u;
        int kk = k;
        while (x < v && kk < sg->nseg) {
            int32_t y = sg->start[kk + 1] < v ? sg->start[kk + 1] : v;
            if (y > x) {
                double w = prefix[y] - prefix[x];
                change += constant * w * (combined(ctx, sg->mask[kk] | extra) - combined(ctx, sg->mask[kk]));
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
    Ev *ev = wk->ev;
    int ne = 0;
    for (int i = 0; i < nrng; i++) {
        int32_t lo = rng[3 * i + 1], hi = rng[3 * i + 2];
        if (lo < 0) lo = 0;
        if (hi > note_count) hi = note_count;
        if (lo >= hi) continue;
        ev[ne].pos = lo; ev[ne].bit = rng[3 * i]; ne++;
        ev[ne].pos = hi; ev[ne].bit = rng[3 * i]; ne++;
    }
    qsort(ev, ne, sizeof(Ev), ev_cmp);
    uint64_t cur = always_mask;
    int ns = 0, e = 0;
    int32_t at = 0;
    for (;;) {
        while (e < ne && ev[e].pos == at) { cur ^= 1ull << ev[e].bit; e++; }
        int32_t next = e < ne ? ev[e].pos : note_count;
        if (next > at && ns < MAX_SEG) { sg->start[ns] = at; sg->mask[ns] = cur; ns++; }
        if (next >= note_count) break;
        at = next;
    }
    sg->nseg = ns;
    sg->start[ns] = note_count;
    double fixed = 0.0;
    for (int s = 0; s < ns; s++) {
        int32_t a = sg->start[s], b = sg->start[s + 1];
        fixed += constant * (prefix[b] - prefix[a]) * (1.0 + combined(ctx, sg->mask[s])) + bias * (double)(b - a);
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

double hd_cdvar(int note_count, const double *times, const double *prefix, double song_seconds,
                double constant, double bias, const double *score_rates, int nrates, double skill_effect_up,
                int nfixed, const int32_t *fixed_i, const double *fixed_d,
                int nactive, const int32_t *active_i, const double *active_d,
                const int32_t *rider_host, const double *rider_q,
                int nhost, const int32_t *host_i, const double *host_d,
                int norder, const int32_t *order, int ncostume, const int32_t *costume,
                int nvar, const double *cd, double *out, const uint8_t *allowed) {
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
