"""NN-based synergy lift: train DeepSetsSolo on mirrored 30k, then for each
candidate X compute:

  nn_with_team(X) = NN P(win | team = [user_4] + [X])
  nn_solo(X)      = NN P(win) averaged over K random 4-teammate samples
  nn_lift(X)      = nn_with_team - nn_solo

Compare to statistical synergy_lift (raw conditional WR) to answer:
  "Does the NN reorder champions vs the pure-stats approach?"
"""
from __future__ import annotations

import math
from pathlib import Path

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import Dataset, DataLoader


# ---- Same DeepSetsSolo as train_single_team.py ----

class SoloDataset(Dataset):
    def __init__(self, df, c2i):
        teams, labels = [], []
        for b, r, w in zip(df["blue_champions"].to_list(), df["red_champions"].to_list(), df["blue_wins"].to_list()):
            teams.append([c2i[c] for c in b]); labels.append(float(w))
            teams.append([c2i[c] for c in r]); labels.append(float(1 - w))
        self.teams = teams
        self.labels = np.array(labels, dtype=np.float32)
    def __len__(self): return len(self.labels)
    def __getitem__(self, i):
        return (torch.tensor(self.teams[i], dtype=torch.long),
                torch.tensor(self.labels[i], dtype=torch.float32))


class DeepSetsSolo(nn.Module):
    def __init__(self, n_champs, embed_dim=16, hidden=32, dropout=0.4):
        super().__init__()
        self.embed = nn.Embedding(n_champs, embed_dim)
        self.mlp = nn.Sequential(
            nn.Linear(embed_dim, hidden),
            nn.LayerNorm(hidden),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, hidden // 2),
            nn.GELU(),
            nn.Linear(hidden // 2, 1),
        )
    def forward(self, team):
        return self.mlp(self.embed(team).sum(dim=1)).squeeze(-1)


def _load_name_map():
    try:
        from aram_nn.lcu.client import LCUClient, get_champion_summary
        from aram_nn.lcu.process import get_credentials
        creds = get_credentials()
        if creds is None: return {}
        with LCUClient(creds) as lcu:
            summary = get_champion_summary(lcu)
        return {int(r.get("id")): (r.get("alias") or r.get("name"))
                for r in summary if r.get("id") is not None}
    except Exception:
        return {}


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patches", multiple=True, default=("16.9", "16.10"))
@click.option("--team", required=True, help="comma-separated 4 aliases")
@click.option("--epochs", default=40, type=int)
@click.option("--lr", default=2e-3, type=float)
@click.option("--wd", default=5e-2, type=float)
@click.option("--n-solo-samples", default=200, type=int, help="random teammate samples for solo baseline")
@click.option("--min-pair", default=30, type=int)
@click.option("--min-total", default=400, type=int)
@click.option("--top", default=15, type=int)
def main(data, patches, team, epochs, lr, wd, n_solo_samples, min_pair, min_total, top):
    torch.manual_seed(42); np.random.seed(42)
    name_map = _load_name_map()
    alias_to_id = {v: k for k, v in name_map.items()}
    team_aliases = [s.strip() for s in team.split(",")]
    team_cid = [alias_to_id[a] for a in team_aliases]

    df = pl.read_parquet(data).filter(pl.col("duration_sec") >= 300)
    df = df.with_columns(pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("p"))
    df = df.filter(pl.col("p").is_in(list(patches)))
    click.echo(f"[data] {df.height} matches")

    # Vocab from all data
    champ_ids = set()
    for r in df["blue_champions"].to_list(): champ_ids.update(r)
    for r in df["red_champions"].to_list():  champ_ids.update(r)
    c2i = {c: i for i, c in enumerate(sorted(champ_ids))}
    n_champs = len(c2i)

    # Time split for val
    df = df.sort("game_creation_ms")
    n = df.height
    n_val = int(n * 0.15)
    df_train = df.slice(0, n - n_val)
    df_val   = df.slice(n - n_val, n_val)

    train_ds = SoloDataset(df_train, c2i)
    val_ds   = SoloDataset(df_val,   c2i)
    click.echo(f"[solo rows] train={len(train_ds)} val={len(val_ds)}")

    train_loader = DataLoader(train_ds, batch_size=256, shuffle=True)
    val_loader   = DataLoader(val_ds,   batch_size=256, shuffle=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DeepSetsSolo(n_champs, embed_dim=16, hidden=32, dropout=0.4).to(device)
    opt = AdamW(model.parameters(), lr=lr, weight_decay=wd)
    sched = CosineAnnealingLR(opt, T_max=epochs)
    crit = nn.BCEWithLogitsLoss()
    best_ll = float("inf"); best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

    click.echo(f"[train] DeepSetsSolo  params={sum(p.numel() for p in model.parameters()):,}")
    for ep in range(1, epochs + 1):
        model.train(); tl, ns = 0.0, 0
        for tt, y in train_loader:
            tt, y = tt.to(device), y.to(device)
            opt.zero_grad(); loss = crit(model(tt), y); loss.backward(); opt.step()
            tl += loss.item() * tt.size(0); ns += tt.size(0)
        sched.step()
        if ep % 5 == 0 or ep == 1:
            model.eval()
            with torch.no_grad():
                ps, ys = [], []
                for tt, y in val_loader:
                    ps.append(torch.sigmoid(model(tt.to(device))).cpu().numpy()); ys.append(y.numpy())
                ps = np.concatenate(ps); ys = np.concatenate(ys)
                ll = -np.mean(ys * np.log(np.clip(ps, 1e-7, 1-1e-7)) + (1-ys) * np.log(np.clip(1-ps, 1e-7, 1-1e-7)))
                acc = ((ps >= 0.5) == ys.astype(bool)).mean()
                click.echo(f"  ep {ep:>2}  train_loss={tl/ns:.4f}  val_ll={ll:.4f}  val_acc={acc:.4f}")
                if ll < best_ll:
                    best_ll = ll; best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)
    model.eval()

    # ---- Candidate set: same filter as synergy_lift ----
    solo_games = {}
    co = {a: {} for a in team_cid}
    rows = []
    for b, r, w in zip(df["blue_champions"].to_list(), df["red_champions"].to_list(), df["blue_wins"].to_list()):
        rows.append((frozenset(b), int(w)))
        rows.append((frozenset(r), int(1 - w)))
    for t, w in rows:
        for c in t: solo_games[c] = solo_games.get(c, 0) + 1
        for a in team_cid:
            if a in t:
                for c in t:
                    if c == a or c in set(team_cid): continue
                    bucket = co[a].setdefault(c, [0, 0]); bucket[0] += 1

    all_candidates = set()
    for a in team_cid: all_candidates.update(co[a].keys())
    all_candidates -= set(team_cid)
    candidates = [c for c in all_candidates if solo_games.get(c, 0) >= min_total
                  and sum(1 for a in team_cid if co[a].get(c, [0])[0] >= min_pair) >= 2]
    click.echo(f"[candidates] {len(candidates)} passed filters")

    # ---- NN predictions ----
    team_idx = torch.tensor([[c2i[c] for c in team_cid]], dtype=torch.long).to(device)  # (1, 4)

    @torch.no_grad()
    def predict_team_with_X(X_cid):
        # 5-champ team: 4 user + X
        team = torch.cat([team_idx, torch.tensor([[c2i[X_cid]]], dtype=torch.long, device=device)], dim=1)
        return float(torch.sigmoid(model(team)).cpu().item())

    @torch.no_grad()
    def predict_solo_X(X_cid, n_samples=200):
        """X with 4 random teammates (sampled uniformly from vocab, excluding X)."""
        all_cids = [cid for cid in c2i if cid != X_cid]
        teams = []
        for _ in range(n_samples):
            tm = np.random.choice(all_cids, size=4, replace=False).tolist()
            teams.append([c2i[X_cid]] + [c2i[c] for c in tm])
        tt = torch.tensor(teams, dtype=torch.long, device=device)
        probs = torch.sigmoid(model(tt)).cpu().numpy()
        return float(probs.mean())

    click.echo(f"\n[NN scoring] {len(candidates)} candidates  ({n_solo_samples} solo samples each)")
    nn_results = []
    for cid in candidates:
        team_pred = predict_team_with_X(cid)
        solo_pred = predict_solo_X(cid, n_solo_samples)
        nn_lift = team_pred - solo_pred
        nn_results.append({
            "id": cid, "name": name_map.get(cid, f"id_{cid}"),
            "team_pred": team_pred, "solo_pred": solo_pred, "nn_lift": nn_lift,
            "n_solo": solo_games[cid],
        })
    nn_results.sort(key=lambda r: -r["nn_lift"])

    # ---- Output ----
    click.echo(f"\n========== NN-BASED SYNERGY LIFT (top {top}) ==========")
    click.echo(f"  {'name':<14} {'team_p':>8} {'solo_p':>8} {'nn_lift':>9} {'n_total':>7}")
    for r in nn_results[:top]:
        click.echo(f"  {r['name']:<14} {r['team_pred']*100:>7.2f}% {r['solo_pred']*100:>7.2f}% "
                   f"{r['nn_lift']*100:>+7.2f}pp {r['n_solo']:>7}")

    click.echo(f"\n========== NN BOTTOM 8 (team drag) ==========")
    for r in nn_results[-8:]:
        click.echo(f"  {r['name']:<14} {r['team_pred']*100:>7.2f}% {r['solo_pred']*100:>7.2f}% "
                   f"{r['nn_lift']*100:>+7.2f}pp {r['n_solo']:>7}")


if __name__ == "__main__":
    main()
