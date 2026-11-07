/* The team-wide Connect socket assignment of `hbuilder.domain.board_plan`, in C, and the
 * team board assembly built on it:
 *
 *   hd_connect   `node_values_from_recipes` per character, the candidate placements' gains
 *                (`_team_connect_assignment_grouped`), the weight matrix and the exact
 *                capacitated matching (`_connect_assignment_exact`, `_max_weight_matching`);
 *   hd_assemble  that matching under the song bonus's per-song cap, as
 *                `board_model_from_plan` runs it on the plan cache: the cap check before the
 *                matching (`_song_bonus_over_cap` with every multiplier at 1), the matching,
 *                the same check after it, and when either binds the capped matching
 *                (`_yellow_capped_assignment`: the room under the cap, the yellow lift, the
 *                bisection on the yellow scale), with the incumbent tie-break
 *                (`INCUMBENT_TIE`) wherever the Python applies it.
 *
 * BIT-IDENTITY IS THE CONTRACT, as in notewalk.c: every expression is written in
 * the order of the Python it replaces, on IEEE-754 doubles, built with
 * -O2 -ffp-contract=off and WITHOUT -ffast-math. The matching is the Python
 * loop line for line -- same scan order, same strict comparisons, the rows of a
 * saturated column kept in arrival order -- so on equal reduced cost it makes
 * the same choice. Sums the Python takes over a frozenset or a dict are replayed in
 * the order the caller packed them in (that set's iteration order, that dict's
 * insertion order); a term that is exactly +0.0 is left out where the Python adds
 * it, which cannot change a sum of non-negative terms. The built-in `sum()` is
 * CPython 3.12+'s compensated one (`hd_py_sum`), the oldest Python the project runs
 * on. tools/web/kernel/test_connect.py and test_assemble.py hold it to `==`.
 *
 * Both return the number of chosen placements, or -1 when a size limit is exceeded
 * (the caller then runs the Python path; never an approximation).
 */
#include <math.h>
#include <stdint.h>
#include <string.h>

#define MAX_CHARS 8
#define MAX_MEMBERS 8
#define MAX_NODES 512     /* valued nodes per character */
#define MAX_ENTRIES 1024  /* socket entries per character */
#define MAX_ROWS 64       /* (character, socket) pairs with a positive placement */
#define MAX_COLS 96       /* distinct (shape, permil) card groups in play */
#define SOCKETS 64        /* socket codes per character */
#define COL_IDS 2048      /* global card-group ranks */
#define MAX_TRIPLES (MAX_CHARS * MAX_ENTRIES)

enum { K_STAT = 0, K_RATE = 1, K_SONG = 2, K_SKILL_EFFECT_UP = 3, K_UPTIME = 4, K_OTHER = 5 };
enum { S_LEADER = 0, S_MEMBER = 1, S_GROUPING = 2, S_ACCOUNT = 3 };
#define F_CONDITIONAL 1
#define F_SONG_SCOPED 2   /* song_scope is "solo" or "group" */
#define F_SOLO 4          /* song_scope is "solo" */
#define F_GROUP 8         /* song_scope is "group" */
/* `song_mask`: the scoped yellows the song pays (F_SOLO and/or F_GROUP;
   both when the scope is not known), `node_values_from_recipes(song_scope=)` */

typedef struct {
    int32_t kind;
    int32_t scope;
    int32_t stat;      /* 0..2, or -1 for "all" */
    int32_t flags;
    double value;
} Eff;

typedef struct {
    int32_t is_override; /* a per-character grouping override row */
    int32_t gen;         /* override: generation bit, or -1 (worth 0.0) */
    int32_t eff_start;
    int32_t eff_count;
    double v;            /* override: its value */
} Node;

typedef struct {
    int32_t socket;      /* code; ascending = the socket ids' string order */
    int32_t col;         /* global rank of the card group (sorted id tuples) */
    int32_t cap;         /* cards in the group */
    int32_t cov_start;
    int32_t cov_count;
    int32_t pad_;
    double permil;
} Entry;

typedef struct {
    int32_t n_nodes;
    int32_t n_entries;
    const Node *nodes;
    const Eff *effs;
    const Entry *entries;
    const int32_t *cov;  /* node index, or -1 for a covered node with no value */
} Plan;

/* `node_values_from_recipes` for one node. */
static double node_value(const Node *nd, const Eff *effs, int is_leader, int own,
                         double singer_weight, int song_mask, int nm, const double *stats,
                         const uint64_t *gen, double dp, double db, double dy, double apr) {
    if (nd->is_override) {
        if (nd->gen < 0) return 0.0;
        int reached = 0;
        for (int m = 0; m < nm; m++)
            if ((gen[m] >> nd->gen) & 1) reached++;
        return nd->v * 3.0 * (double)reached * dp;
    }
    double best = 0.0;
    for (int k = 0; k < nd->eff_count; k++) {
        const Eff *e = &effs[nd->eff_start + k];
        int nreach, own_only = 0;
        if (e->scope == S_LEADER) nreach = is_leader ? nm : 0;
        else if (e->scope == S_MEMBER) { nreach = own >= 0 ? 1 : 0; own_only = 1; }
        else if (e->scope == S_GROUPING) continue;
        else nreach = nm;
        double row = 0.0;
        int nstat = e->stat < 0 ? 3 : 1;
        if (e->kind == K_RATE) {
            for (int r = 0; r < nreach; r++) {
                const double *cs = stats + 3 * (own_only ? own : r);
                for (int s = 0; s < nstat; s++) {
                    int st = e->stat < 0 ? s : e->stat;
                    row += ceil(e->value * cs[st]) * dp;
                }
            }
        } else if (e->kind == K_STAT) {
            double add = e->value * dp;
            for (int i = 0; i < nreach * nstat; i++) row += add;
        } else if (e->kind == K_SONG) {
            /* `dy`, the song's score rate (it multiplies the whole Unit Score); the
               pooled `all` yellows pay only when the song's scope is not solo/group */
            if (nreach) row += e->value * dy * ((e->flags & F_SONG_SCOPED)
                                                ? ((e->flags & song_mask) ? singer_weight : 0.0)
                                                : ((song_mask == F_SOLO || song_mask == F_GROUP)
                                                   ? 0.0 : 1.0));
        } else if (e->kind == K_SKILL_EFFECT_UP) {
            if (nreach) row += apr * e->value * db;
        } else if (e->kind == K_UPTIME) {
            if (own >= 0 && nm) row += (apr / (double)nm) * e->value * db;
        }
        if (e->flags & F_CONDITIONAL) row *= singer_weight;
        if (row > best) best = row;
    }
    return best;
}

/* ------------------------------------------------------------------ shared state */

/* One call's characters, set by each entry point (the call holds the GIL: PyDLL). */
static int g_n;
static const Plan *g_plans;
static const int32_t *g_rank;
/* every character's base node values, unscaled, and which of its nodes are paying yellows */
static double g_values[MAX_CHARS][MAX_NODES];
static unsigned char g_paying[MAX_CHARS][MAX_NODES];

static int values_for(int n_chars, const Plan *plans, const int32_t *char_rank,
                      const int32_t *is_leader, const int32_t *own, const double *singer_weight,
                      int song_mask, int nm, const double *stats, const uint64_t *gen,
                      double dp, double db, double dy, double apr) {
    if (n_chars > MAX_CHARS || nm > MAX_MEMBERS) return -1;
    g_n = n_chars;
    g_plans = plans;
    g_rank = char_rank;
    for (int c = 0; c < n_chars; c++) {
        const Plan *p = &plans[c];
        if (p->n_nodes > MAX_NODES || p->n_entries > MAX_ENTRIES) return -1;
        if (char_rank[c] < 0 || char_rank[c] >= MAX_CHARS) return -1;
        for (int i = 0; i < p->n_nodes; i++) {
            g_values[c][i] = node_value(&p->nodes[i], p->effs, is_leader[c], own[c],
                                        singer_weight[c], song_mask, nm, stats, gen, dp, db, dy, apr);
            g_paying[c][i] = 0;
        }
    }
    return 0;
}

/* One matching's answer: the matched placements in row order with their gains, and the
 * multipliers they give each character's valued nodes, with those nodes in the order
 * `_placements_from_matched` files them in its dict (the placements by gain desc, then
 * character, then socket; each placement's covered nodes in shape order). */
typedef struct {
    int n;
    int32_t ch[MAX_ROWS], en[MAX_ROWS];
    double gain[MAX_ROWS];        /* the placement's gain; the incumbent tie raises only its weight */
    double mult[MAX_CHARS][MAX_NODES];
    int16_t order[MAX_CHARS][MAX_NODES];
    int n_order[MAX_CHARS];
} Answer;

static Answer g_ans[2];

/* `_connect_assignment_exact` over the current gains: `g_values`, the paying yellows times
 * `scale` when `scaled`. A placement whose (character, socket) would keep its own card
 * (`tie[c][e]` nonzero; `tie` NULL: no incumbents) weighs `gain * tie_factor`
 * (`INCUMBENT_TIE`); the gain kept is the true one. -1 on a size limit. */
static int solve(const uint8_t *const *tie, double tie_factor, int scaled, double scale, Answer *a) {
    static double weights[MAX_ROWS][MAX_COLS + 1];
    static double at_gain[MAX_ROWS][MAX_COLS];
    static double cost[MAX_ROWS][MAX_COLS + 1];
    static int32_t at_char[MAX_ROWS][MAX_COLS], at_entry[MAX_ROWS][MAX_COLS];
    static int16_t row_of[MAX_CHARS * SOCKETS], col_of[COL_IDS];
    static int32_t col_cap[COL_IDS];
    /* candidate placements with gain > 0, in the Python's triple order */
    static int32_t t_char[MAX_TRIPLES], t_entry[MAX_TRIPLES], t_row[MAX_TRIPLES], t_col[MAX_TRIPLES];
    static double t_gain[MAX_TRIPLES], t_weight[MAX_TRIPLES];
    static int matched[MAX_COLS + 1][MAX_ROWS];
    int nt = 0;

    a->n = 0;
    memset(row_of, 0, sizeof row_of);
    memset(col_of, 0, sizeof col_of);
    for (int c = 0; c < g_n; c++) {
        const Plan *p = &g_plans[c];
        const uint8_t *ties = tie != NULL ? tie[c] : NULL;
        for (int e = 0; e < p->n_entries; e++) {
            const Entry *en = &p->entries[e];
            double gain = 0.0;
            for (int k = 0; k < en->cov_count; k++) {
                int idx = p->cov[en->cov_start + k];
                double val = idx >= 0 ? g_values[c][idx] : 0.0;
                if (scaled && idx >= 0 && g_paying[c][idx]) val = val * scale;
                gain += val * en->permil / 1000.0;
            }
            if (gain > 0) {
                if (en->socket < 0 || en->socket >= SOCKETS) return -1;
                if (en->col < 0 || en->col >= COL_IDS) return -1;
                int rk = g_rank[c] * SOCKETS + en->socket;
                row_of[rk] = 1;
                col_of[en->col] = 1;
                col_cap[en->col] = en->cap;
                t_char[nt] = c; t_entry[nt] = e; t_row[nt] = rk; t_col[nt] = en->col;
                t_gain[nt] = gain;
                t_weight[nt] = (ties != NULL && ties[e]) ? gain * tie_factor : gain;
                nt++;
            }
        }
    }
    if (nt == 0) return 0;

    /* rows and columns in sorted-key order, only those with a placement */
    int n = 0, ncol = 0;
    int caps[MAX_COLS + 1];
    for (int k = 0; k < MAX_CHARS * SOCKETS; k++)
        if (row_of[k]) { if (n >= MAX_ROWS) return -1; row_of[k] = (int16_t)n++; } else row_of[k] = -1;
    for (int k = 0; k < COL_IDS; k++)
        if (col_of[k]) { if (ncol >= MAX_COLS) return -1; caps[ncol] = col_cap[k]; col_of[k] = (int16_t)ncol++; }
        else col_of[k] = -1;

    for (int i = 0; i < n; i++)
        for (int j = 0; j < ncol; j++) weights[i][j] = 0.0;
    for (int t = 0; t < nt; t++) {
        int i = row_of[t_row[t]], j = col_of[t_col[t]];
        if (t_weight[t] > weights[i][j]) {
            weights[i][j] = t_weight[t];
            at_gain[i][j] = t_gain[t];
            at_char[i][j] = t_char[t];
            at_entry[i][j] = t_entry[t];
        }
    }

    /* `_max_weight_matching`, line for line */
    int m = ncol + 1;
    caps[ncol] = n;
    const double inf = INFINITY;
    double u[MAX_ROWS], v[MAX_COLS + 1], minv[MAX_COLS + 1];
    int way_col[MAX_COLS + 1], way_row[MAX_COLS + 1], used[MAX_COLS + 1];
    int visited[MAX_COLS + 1];
    int matched_n[MAX_COLS + 1];
    int col_of_row[MAX_ROWS];
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < ncol; j++) cost[i][j] = -weights[i][j];
        cost[i][ncol] = 0.0;
        u[i] = 0.0;
        col_of_row[i] = -1;
    }
    for (int j = 0; j < m; j++) { v[j] = 0.0; matched_n[j] = 0; }

    for (int r = 0; r < n; r++) {
        int nvisited = 0;
        const double *cr = cost[r];
        double ur = u[r];
        for (int j = 0; j < m; j++) { minv[j] = inf; way_col[j] = -1; way_row[j] = -1; used[j] = 0; }
        for (int j = 0; j < m; j++) {
            double c = cr[j] - ur - v[j];
            if (c < minv[j]) { minv[j] = c; way_row[j] = r; }
        }
        int j1;
        for (;;) {
            double delta = inf;
            j1 = -1;
            for (int j = 0; j < m; j++)
                if (!used[j] && minv[j] < delta) { delta = minv[j]; j1 = j; }
            if (j1 < 0) return -1;
            u[r] += delta;
            for (int k = 0; k < nvisited; k++) {
                int j = visited[k];
                v[j] -= delta;
                for (int q = 0; q < matched_n[j]; q++) u[matched[j][q]] += delta;
            }
            for (int j = 0; j < m; j++)
                if (!used[j]) minv[j] -= delta;
            used[j1] = 1;
            visited[nvisited++] = j1;
            if (matched_n[j1] < caps[j1]) break;
            for (int q = 0; q < matched_n[j1]; q++) {
                int i = matched[j1][q];
                const double *ci = cost[i];
                double ui = u[i];
                for (int j = 0; j < m; j++)
                    if (!used[j]) {
                        double c = ci[j] - ui - v[j];
                        if (c < minv[j]) { minv[j] = c; way_col[j] = j1; way_row[j] = i; }
                    }
            }
        }
        int j = j1;
        for (;;) {
            int i = way_row[j], pj = way_col[j];
            matched[j][matched_n[j]++] = i;
            col_of_row[i] = j;
            if (pj == -1) break;
            /* matched[pj].remove(i): drop the first occurrence, keep the order */
            int q = 0;
            while (q < matched_n[pj] && matched[pj][q] != i) q++;
            for (; q + 1 < matched_n[pj]; q++) matched[pj][q] = matched[pj][q + 1];
            matched_n[pj]--;
            j = pj;
        }
    }
    for (int i = 0; i < n; i++) {
        int j = col_of_row[i];
        if (j >= 0 && j < ncol && weights[i][j] > 0) {
            a->ch[a->n] = at_char[i][j];
            a->en[a->n] = at_entry[i][j];
            a->gain[a->n] = at_gain[i][j];
            a->n++;
        }
    }
    return a->n;
}

static int emit(const Answer *a, int32_t *out_char, int32_t *out_entry, double *out_gain) {
    for (int k = 0; k < a->n; k++) {
        out_char[k] = a->ch[k];
        out_entry[k] = a->en[k];
        out_gain[k] = a->gain[k];
    }
    return a->n;
}

int hd_connect(int n_chars, const Plan *plans, const int32_t *char_rank,
               const int32_t *is_leader, const int32_t *own, const double *singer_weight,
               int song_mask, int nm, const double *stats, const uint64_t *gen,
               double dp, double db, double dy, double apr,
               int32_t *out_char, int32_t *out_entry, double *out_gain) {
    if (values_for(n_chars, plans, char_rank, is_leader, own, singer_weight, song_mask, nm,
                   stats, gen, dp, db, dy, apr) < 0)
        return -1;
    Answer *a = &g_ans[0];
    if (solve(NULL, 1.0, 0, 0.0, a) < 0) return -1;
    return emit(a, out_char, out_entry, out_gain);
}

/* ------------------------------------------------- the assembly under the song cap */

/* One character's paying yellows (`_yellow_permil` != 0) in its `selected` set's iteration
 * order: the node's index among the plan's valued nodes (-1 when no socket entry covers it, so
 * its multiplier is always 1) and its permil; `pay_sum` is `sum(rows.values())`. */
typedef struct {
    int32_t n_pay;
    int32_t pad_;
    const int32_t *pay_idx;
    const double *pay_p;
    double pay_sum;
} Yel;

/* `_placements_from_matched`'s multipliers and their dict order: the placements sorted by
 * (-gain, character, socket) -- Python's sort is stable and these keys are unique -- each
 * covered node set to max(its multiplier, 1 + permil / 1000) and filed on first touch. */
static void multipliers(Answer *a) {
    int idx[MAX_ROWS];
    static unsigned char seen[MAX_CHARS][MAX_NODES];
    for (int k = 0; k < a->n; k++) idx[k] = k;
    for (int k = 1; k < a->n; k++) {          /* insertion sort, stable */
        int x = idx[k], q = k - 1;
        while (q >= 0) {
            int y = idx[q];
            int after;                         /* does y sort after x? */
            if (a->gain[y] != a->gain[x]) after = a->gain[y] < a->gain[x];
            else if (g_rank[a->ch[y]] != g_rank[a->ch[x]]) after = g_rank[a->ch[y]] > g_rank[a->ch[x]];
            else after = g_plans[a->ch[y]].entries[a->en[y]].socket
                         > g_plans[a->ch[x]].entries[a->en[x]].socket;
            if (!after) break;
            idx[q + 1] = y;
            q--;
        }
        idx[q + 1] = x;
    }
    for (int c = 0; c < g_n; c++) {
        a->n_order[c] = 0;
        for (int i = 0; i < g_plans[c].n_nodes; i++) a->mult[c][i] = 1.0;
        memset(seen[c], 0, (size_t)g_plans[c].n_nodes);
    }
    for (int k = 0; k < a->n; k++) {
        int c = a->ch[idx[k]];
        const Plan *p = &g_plans[c];
        const Entry *en = &p->entries[a->en[idx[k]]];
        double mu = 1.0 + en->permil / 1000.0;
        for (int q = 0; q < en->cov_count; q++) {
            int i = p->cov[en->cov_start + q];
            if (i < 0) continue;               /* no value, multiplied or not: adds +0.0 */
            if (!seen[c][i]) { seen[c][i] = 1; a->order[c][a->n_order[c]++] = (int16_t)i; }
            if (mu > a->mult[c][i]) a->mult[c][i] = mu;
        }
    }
}

/* `_song_bonus_over_cap`'s total: the team's paying yellows at their multipliers (`a`, or 1
 * when NULL), then the off-team rows of boards not on the team. */
static double yellow_total(const Yel *yels, const Answer *a, int nb, const int32_t *bench_who,
                           const double *bench_val, const int32_t *team_code) {
    double total = 0.0;
    for (int c = 0; c < g_n; c++) {
        const Yel *y = &yels[c];
        for (int k = 0; k < y->n_pay; k++) {
            int i = y->pay_idx[k];
            double mu = (a != NULL && i >= 0) ? a->mult[c][i] : 1.0;
            total += y->pay_p[k] * mu;
        }
    }
    for (int b = 0; b < nb; b++) {
        int on_team = 0;
        for (int c = 0; c < g_n; c++)
            if (team_code[c] == bench_who[b]) { on_team = 1; break; }
        if (!on_team) total += bench_val[b];
    }
    return total;
}

/* The built-in `sum()` over floats from its int 0 start, as CPython 3.12+ computes it: the
 * first item added to 0 exactly, every later one with Neumaier's compensation, and the
 * compensation added at the end when it is non-zero and finite (`builtin_sum_impl`). */
double hd_py_sum(const double *x, int n) {
    if (n == 0) return 0.0;
    double f = 0.0 + x[0], c = 0.0;
    for (int k = 1; k < n; k++) {
        double t = f + x[k];
        if (fabs(f) >= fabs(x[k])) c += (f - t) + x[k];
        else c += (x[k] - t) + f;
        f = t;
    }
    if (c != 0.0 && isfinite(c)) f += c;
    return f;
}

#define MAX_PAY (MAX_CHARS * MAX_NODES)

/* `lift_of`: the yellow permil the answer's multipliers add, characters in team order, as
 * the Python's `sum(...)` over them. */
static double lift_of(const Yel *yels, const Answer *a) {
    static double terms[MAX_PAY];
    int n = 0;
    for (int c = 0; c < g_n; c++) {
        const Yel *y = &yels[c];
        for (int k = 0; k < y->n_pay; k++) {
            int i = y->pay_idx[k];
            double mu = i >= 0 ? a->mult[c][i] : 1.0;
            terms[n++] = (mu - 1.0) * y->pay_p[k];
        }
    }
    return hd_py_sum(terms, n);
}

/* `worth`: the answer's gain on the unscaled values, its yellow part capped at `room`. */
static double worth(const Yel *yels, const Answer *a, double room) {
    double other = 0.0, paid = 0.0;
    for (int c = 0; c < g_n; c++) {
        for (int q = 0; q < a->n_order[c]; q++) {
            int i = a->order[c][q];
            double mu = a->mult[c][i];
            if (g_paying[c][i]) paid += (mu - 1.0) * g_values[c][i];
            else other += (mu - 1.0) * g_values[c][i];
        }
    }
    double lift = lift_of(yels, a);
    return other + (lift <= room + 1e-9 ? paid : paid * room / lift);
}

/* `board_model_from_plan`'s Connect matching for one team with the song's cast known:
 *
 *   - with `checks` (a plan the cache made: the fast path's flow) the cap check with every
 *     multiplier at 1; when it does not bind, the plan cache's matching (the incumbent tie
 *     only when `tie_first`: a call that names the cards) and the same check with its
 *     multipliers;
 *   - when either binds, or without `checks` (a plan the cache did not make: the plain
 *     path's flow, straight to it), `_yellow_capped_assignment` (every matching with the
 *     incumbent tie): the room under `cap`, the yellow lift, and the bisection on the yellow
 *     scale with up to `rounds` more matchings, keeping the one worth most.
 *
 * `yels` per character; `team_code` its board's code, `bench_who` / `bench_val` the off-team
 * boards' paying yellow rows (code, permil x their Connect multiplier) -- a row whose board is
 * on the team is skipped. Returns the number of chosen placements (row order), or -1 on a size
 * limit. out_info: [0] the path (0: no cap binds, 1: capped, its first matching kept, 2:
 * capped, a scaled matching kept), [1] the matchings solved, [2] the check that bound (0 none,
 * 1 before the matching, 2 after it, 3 no checks). */
int hd_assemble(int n_chars, const Plan *plans, const Yel *yels, const int32_t *char_rank,
                const int32_t *team_code, const int32_t *is_leader, const int32_t *own,
                const double *singer_weight, int song_mask, int nm, const double *stats,
                const uint64_t *gen, double dp, double db, double dy, double apr,
                int nb, const int32_t *bench_who, const double *bench_val,
                double cap, int rounds, const uint8_t *const *tie, double tie_factor, int tie_first,
                int checks, int32_t *out_char, int32_t *out_entry, double *out_gain,
                int32_t *out_info) {
    out_info[0] = out_info[1] = out_info[2] = 0;
    if (values_for(n_chars, plans, char_rank, is_leader, own, singer_weight, song_mask, nm,
                   stats, gen, dp, db, dy, apr) < 0)
        return -1;
    for (int c = 0; c < n_chars; c++) {
        if (yels[c].n_pay > MAX_NODES) return -1;
        for (int k = 0; k < yels[c].n_pay; k++) {
            int i = yels[c].pay_idx[k];
            if (i >= plans[c].n_nodes) return -1;
            if (i >= 0) g_paying[c][i] = 1;
        }
    }
    Answer *best = &g_ans[0], *cand = &g_ans[1];

    /* the check before the matching, every multiplier at 1: when it binds (or there are no
       checks), the matching below is already the capped path's first one */
    int bound = !checks ? 3 : yellow_total(yels, NULL, nb, bench_who, bench_val, team_code) > cap + 1e-9;
    if (solve(bound || tie_first ? tie : NULL, tie_factor, 0, 0.0, best) < 0) return -1;
    out_info[1] = 1;
    multipliers(best);
    if (!bound) {
        if (!(yellow_total(yels, best, nb, bench_who, bench_val, team_code) > cap + 1e-9))
            return emit(best, out_char, out_entry, out_gain);
        bound = 2;
        if (tie != NULL && !tie_first) {      /* the capped path's own first matching */
            if (solve(tie, tie_factor, 0, 0.0, best) < 0) return -1;
            out_info[1]++;
            multipliers(best);
        }
    }
    out_info[2] = bound;
    out_info[0] = 1;

    /* `_yellow_capped_assignment`: the room the cap leaves */
    double room = cap;
    for (int b = 0; b < nb; b++) {
        int on_team = 0;
        for (int c = 0; c < g_n; c++)
            if (team_code[c] == bench_who[b]) { on_team = 1; break; }
        if (!on_team) room -= bench_val[b];
    }
    int any_yellow = 0;
    for (int c = 0; c < g_n; c++)
        if (yels[c].n_pay > 0) { any_yellow = 1; room -= yels[c].pay_sum; }
    if (!any_yellow) return emit(best, out_char, out_entry, out_gain);
    if (0.0 > room) room = 0.0;                        /* max(room, 0.0) */
    double lift = lift_of(yels, best);
    if (lift <= room + 1e-9) return emit(best, out_char, out_entry, out_gain);
    /* ties go to the smaller yellow lift */
    double best_worth = worth(yels, best, room), best_neg = -lift;
    double low = 0.0, high = 1.0, scale = room / lift;
    for (int r = 0; r < rounds; r++) {
        if (solve(tie, tie_factor, 1, scale, cand) < 0) return -1;
        out_info[1]++;
        multipliers(cand);
        double c_lift = lift_of(yels, cand);
        double k0 = worth(yels, cand, room), k1 = -c_lift;
        if (k0 > best_worth + 1e-9 || (k0 >= best_worth - 1e-9 && k1 > best_neg)) {
            Answer *t = best; best = cand; cand = t;
            best_worth = k0; best_neg = k1;
            out_info[0] = 2;
        }
        if (c_lift > room + 1e-9) high = scale;
        else low = scale;
        if (high - low < 1e-3) break;
        scale = (low + high) / 2.0;
    }
    return emit(best, out_char, out_entry, out_gain);
}
