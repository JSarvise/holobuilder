/* The team-wide Connect socket assignment of `hbuilder.domain.board_plan`, in C:
 * `node_values_from_recipes` per character, the candidate placements' gains
 * (`_team_connect_assignment_grouped`), the weight matrix and the exact
 * capacitated matching (`_connect_assignment_exact`, `_max_weight_matching`).
 *
 * BIT-IDENTITY IS THE CONTRACT, as in notewalk.c: every expression is written in
 * the order of the Python it replaces, on IEEE-754 doubles, built with
 * -O2 -ffp-contract=off and WITHOUT -ffast-math. The matching is the Python
 * loop line for line -- same scan order, same strict comparisons, the rows of a
 * saturated column kept in arrival order -- so on equal reduced cost it makes
 * the same choice. tools/web/kernel/test_connect.py holds it to `==`.
 *
 * Returns the number of chosen placements, or -1 when a size limit is exceeded
 * (the caller then runs the Python path; never an approximation).
 */
#include <math.h>
#include <stdint.h>
#include <string.h>

#define MAX_CHARS 8
#define MAX_MEMBERS 8
#define MAX_NODES 512     /* valued nodes per character */
#define MAX_ROWS 64       /* (character, socket) pairs with a positive placement */
#define MAX_COLS 96       /* distinct (shape, permil) card groups in play */
#define SOCKETS 64        /* socket codes per character */
#define COL_IDS 2048      /* global card-group ranks */

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

int hd_connect(int n_chars, const Plan *plans, const int32_t *char_rank,
               const int32_t *is_leader, const int32_t *own, const double *singer_weight,
               int song_mask, int nm, const double *stats, const uint64_t *gen,
               double dp, double db, double dy, double apr,
               int32_t *out_char, int32_t *out_entry, double *out_gain) {
    static double values[MAX_NODES];
    static double weights[MAX_ROWS][MAX_COLS + 1];
    static double cost[MAX_ROWS][MAX_COLS + 1];
    static int32_t at_char[MAX_ROWS][MAX_COLS], at_entry[MAX_ROWS][MAX_COLS];
    static int16_t row_of[MAX_CHARS * SOCKETS], col_of[COL_IDS];
    static int32_t col_cap[COL_IDS];
    /* candidate placements with gain > 0, in the Python's triple order */
    static int32_t t_char[MAX_CHARS * 1024], t_entry[MAX_CHARS * 1024];
    static int32_t t_row[MAX_CHARS * 1024], t_col[MAX_CHARS * 1024];
    static double t_gain[MAX_CHARS * 1024];
    int nt = 0;

    if (n_chars > MAX_CHARS || nm > MAX_MEMBERS) return -1;
    memset(row_of, 0, sizeof row_of);
    memset(col_of, 0, sizeof col_of);

    for (int c = 0; c < n_chars; c++) {
        const Plan *p = &plans[c];
        if (p->n_nodes > MAX_NODES || p->n_entries > 1024) return -1;
        if (char_rank[c] < 0 || char_rank[c] >= MAX_CHARS) return -1;
        for (int i = 0; i < p->n_nodes; i++)
            values[i] = node_value(&p->nodes[i], p->effs, is_leader[c], own[c],
                                   singer_weight[c], song_mask, nm, stats, gen, dp, db, dy, apr);
        for (int e = 0; e < p->n_entries; e++) {
            const Entry *en = &p->entries[e];
            double gain = 0.0;
            for (int k = 0; k < en->cov_count; k++) {
                int idx = p->cov[en->cov_start + k];
                double val = idx >= 0 ? values[idx] : 0.0;
                gain += val * en->permil / 1000.0;
            }
            if (gain > 0) {
                if (en->socket < 0 || en->socket >= SOCKETS) return -1;
                if (en->col < 0 || en->col >= COL_IDS) return -1;
                int rk = char_rank[c] * SOCKETS + en->socket;
                row_of[rk] = 1;
                col_of[en->col] = 1;
                col_cap[en->col] = en->cap;
                t_char[nt] = c; t_entry[nt] = e; t_row[nt] = rk; t_col[nt] = en->col;
                t_gain[nt] = gain;
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
        if (t_gain[t] > weights[i][j]) {
            weights[i][j] = t_gain[t];
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
    static int matched[MAX_COLS + 1][MAX_ROWS];
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

    int out = 0;
    for (int i = 0; i < n; i++) {
        int j = col_of_row[i];
        if (j >= 0 && j < ncol && weights[i][j] > 0) {
            out_char[out] = at_char[i][j];
            out_entry[out] = at_entry[i][j];
            out_gain[out] = weights[i][j];
            out++;
        }
    }
    return out;
}
