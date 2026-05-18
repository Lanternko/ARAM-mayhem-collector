"""Train DeepSets with champion ability-derived static features.

The experiment compares, on the same time-based split:

  - constant blue-side base-rate baseline
  - logistic regression on champion presence
  - DeepSets using learned champion embeddings only
  - DeepSets using learned embeddings plus Data Dragon ability features

Recommended first run:

    python scripts/train_ability_nn.py \
        --data data/raw/mayhem_30k.parquet \
        --ability-json data/cache/champion_abilities.json \
        --patch-prefix 16.10 \
        --out models/ability_nn_16_10
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import numpy as np
import polars as pl
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader, Dataset

from aram_nn.data import ARAMDataset
from aram_nn.eval import accuracy_np, ece_np, log_loss_np
from aram_nn.models.logreg import train_and_eval as lr_train_eval


ABILITY_TAGS = (
    "hard_cc",
    "soft_cc",
    "mobility",
    "aoe_or_multitarget",
    "shield",
    "heal_or_sustain",
    "poke_hint",
    "execute_or_missing_health",
)


def _to_float_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)) and math.isfinite(float(item)):
            out.append(float(item))
    return out


def _last_number(value: Any, default: float = 0.0) -> float:
    xs = _to_float_list(value)
    return xs[-1] if xs else default


def _mean_number(value: Any, default: float = 0.0) -> float:
    xs = _to_float_list(value)
    return float(sum(xs) / len(xs)) if xs else default


def _range_value(ability: dict[str, Any]) -> float:
    xs = [x for x in _to_float_list(ability.get("range")) if 0 < x < 10000]
    if xs:
        return max(xs)
    raw = str(ability.get("range_burn") or "")
    nums = []
    for part in raw.split("/"):
        try:
            v = float(part)
        except ValueError:
            continue
        if 0 < v < 10000:
            nums.append(v)
    return max(nums) if nums else 0.0


def build_ability_feature_map(ability_json: Path) -> tuple[dict[int, np.ndarray], list[str]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    feature_names: list[str] = []
    for slot in ("Q", "W", "E", "R"):
        feature_names.extend(
            [
                f"{slot}_range",
                f"{slot}_cooldown_rank1",
                f"{slot}_cooldown_maxrank",
                f"{slot}_cooldown_mean",
                f"{slot}_cost_mean",
            ]
        )
        feature_names.extend(f"{slot}_tag_{tag}" for tag in ABILITY_TAGS)
    feature_names.extend(
        [
            "spell_range_mean",
            "spell_range_max",
            "spell_long_range_count",
            "spell_low_cd_count",
            "spell_has_no_cost_count",
        ]
    )
    feature_names.extend(f"tag_count_{tag}" for tag in ABILITY_TAGS)

    out: dict[int, np.ndarray] = {}
    for champion in raw.get("champions", []):
        cid = int(champion["champion_id"])
        by_slot = {ability.get("slot"): ability for ability in champion.get("abilities", [])}
        values: list[float] = []
        ranges: list[float] = []
        tag_counts = {tag: 0.0 for tag in ABILITY_TAGS}
        long_range_count = 0.0
        low_cd_count = 0.0
        no_cost_count = 0.0

        for slot in ("Q", "W", "E", "R"):
            ability = by_slot.get(slot, {})
            cd = _to_float_list(ability.get("cooldown"))
            cost = _to_float_list(ability.get("cost"))
            r = _range_value(ability)
            ranges.append(r)
            if r >= 900:
                long_range_count += 1.0
            if cd and min(cd) <= 6.0:
                low_cd_count += 1.0
            if cost and max(cost) <= 0.0:
                no_cost_count += 1.0

            tags = set(ability.get("heuristic_tags") or [])
            for tag in tags:
                if tag in tag_counts:
                    tag_counts[tag] += 1.0

            values.extend(
                [
                    r,
                    _last_number(cd[:1], 0.0),
                    _last_number(cd, 0.0),
                    _mean_number(cd, 0.0),
                    _mean_number(cost, 0.0),
                ]
            )
            values.extend(1.0 if tag in tags else 0.0 for tag in ABILITY_TAGS)

        values.extend(
            [
                float(sum(ranges) / len(ranges)) if ranges else 0.0,
                float(max(ranges)) if ranges else 0.0,
                long_range_count,
                low_cd_count,
                no_cost_count,
            ]
        )
        values.extend(tag_counts[tag] for tag in ABILITY_TAGS)
        out[cid] = np.asarray(values, dtype=np.float32)

    return out, feature_names


def build_vocab(df: pl.DataFrame) -> dict[int, int]:
    ids: set[int] = set()
    for row in df["blue_champions"].to_list():
        ids.update(int(x) for x in row)
    for row in df["red_champions"].to_list():
        ids.update(int(x) for x in row)
    return {cid: i for i, cid in enumerate(sorted(ids))}


class TeamDataset(Dataset):
    def __init__(self, df: pl.DataFrame, champ_to_idx: dict[int, int]):
        self.blue = [[champ_to_idx[int(c)] for c in row] for row in df["blue_champions"].to_list()]
        self.red = [[champ_to_idx[int(c)] for c in row] for row in df["red_champions"].to_list()]
        self.labels = df["blue_wins"].cast(pl.Float32).to_numpy()

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, i: int):
        return (
            torch.tensor(self.blue[i], dtype=torch.long),
            torch.tensor(self.red[i], dtype=torch.long),
            torch.tensor(self.labels[i], dtype=torch.float32),
        )


@dataclass
class SplitData:
    train: TeamDataset
    val: TeamDataset
    test: TeamDataset
    train_lr: ARAMDataset
    val_lr: ARAMDataset
    test_lr: ARAMDataset
    champ_to_idx: dict[int, int]
    blue_base_rate: float
    rows_before_known_filter: tuple[int, int, int]


def load_split_data(
    data: Path,
    patch_prefix: str,
    *,
    min_duration: int = 300,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
) -> SplitData:
    df = pl.read_parquet(data)
    df = df.filter(pl.col("duration_sec") >= min_duration)
    if patch_prefix:
        df = df.with_columns(
            pl.col("patch").str.split(".").list.slice(0, 2).list.join(".").alias("patch_prefix")
        ).filter(pl.col("patch_prefix") == patch_prefix)

    if df.height == 0:
        raise click.ClickException("No rows after data filters")

    df = df.sort("game_creation_ms")
    n = df.height
    n_test = max(1, int(n * test_frac))
    n_val = max(1, int(n * val_frac))
    n_train = n - n_test - n_val
    df_train = df.slice(0, n_train)
    df_val = df.slice(n_train, n_val)
    df_test = df.slice(n_train + n_val, n_test)
    before = (df_train.height, df_val.height, df_test.height)

    champ_to_idx = build_vocab(df_train)
    known = set(champ_to_idx)

    def filter_known(d: pl.DataFrame) -> pl.DataFrame:
        mask = (
            d["blue_champions"].list.eval(pl.element().is_in(list(known))).list.all()
            & d["red_champions"].list.eval(pl.element().is_in(list(known))).list.all()
        )
        return d.filter(mask)

    df_val = filter_known(df_val)
    df_test = filter_known(df_test)

    return SplitData(
        train=TeamDataset(df_train, champ_to_idx),
        val=TeamDataset(df_val, champ_to_idx),
        test=TeamDataset(df_test, champ_to_idx),
        train_lr=ARAMDataset(df_train, champ_to_idx),
        val_lr=ARAMDataset(df_val, champ_to_idx),
        test_lr=ARAMDataset(df_test, champ_to_idx),
        champ_to_idx=champ_to_idx,
        blue_base_rate=float(df_train["blue_wins"].mean()),
        rows_before_known_filter=before,
    )


def make_ability_matrix(
    champ_to_idx: dict[int, int],
    feature_map: dict[int, np.ndarray],
) -> tuple[torch.Tensor, list[int]]:
    if not feature_map:
        raise click.ClickException("Ability feature map is empty")
    dim = len(next(iter(feature_map.values())))
    mat = np.zeros((len(champ_to_idx), dim), dtype=np.float32)
    missing: list[int] = []
    for cid, idx in champ_to_idx.items():
        vec = feature_map.get(cid)
        if vec is None:
            missing.append(cid)
            continue
        mat[idx] = vec

    mean = mat.mean(axis=0, keepdims=True)
    std = mat.std(axis=0, keepdims=True)
    mat = (mat - mean) / np.where(std < 1e-6, 1.0, std)
    return torch.tensor(mat, dtype=torch.float32), missing


def make_loader(ds: Dataset, batch_size: int, shuffle: bool) -> DataLoader:
    return DataLoader(ds, batch_size=batch_size, shuffle=shuffle, drop_last=False)


def _mlp(in_dim: int, hidden: int, dropout: float) -> nn.Sequential:
    return nn.Sequential(
        nn.Linear(in_dim, hidden),
        nn.LayerNorm(hidden),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(hidden, hidden // 2),
        nn.GELU(),
        nn.Linear(hidden // 2, 1),
    )


class DeepSetsAbility(nn.Module):
    def __init__(
        self,
        n_champs: int,
        ability_features: torch.Tensor | None,
        *,
        embed_dim: int,
        ability_dim: int,
        hidden: int,
        dropout: float,
    ):
        super().__init__()
        self.embed = nn.Embedding(n_champs, embed_dim)
        if ability_features is None:
            self.register_buffer("ability_features", torch.zeros(n_champs, 0))
            self.ability_proj = None
            repr_dim = embed_dim
        else:
            self.register_buffer("ability_features", ability_features)
            self.ability_proj = nn.Sequential(
                nn.Linear(ability_features.shape[1], ability_dim),
                nn.LayerNorm(ability_dim),
                nn.GELU(),
                nn.Dropout(dropout),
            )
            repr_dim = embed_dim + ability_dim
        self.mlp = _mlp(2 * repr_dim, hidden, dropout)

    def champion_repr(self, champ_ids: torch.Tensor) -> torch.Tensor:
        emb = self.embed(champ_ids)
        if self.ability_proj is None:
            return emb
        static = self.ability_proj(self.ability_features[champ_ids])
        return torch.cat([emb, static], dim=-1)

    def _raw_logit(self, diff: torch.Tensor, total: torch.Tensor) -> torch.Tensor:
        return self.mlp(torch.cat([diff, total], dim=-1)).squeeze(-1)

    def forward(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        e_b = self.champion_repr(blue).sum(dim=1)
        e_r = self.champion_repr(red).sum(dim=1)
        diff = e_b - e_r
        total = e_b + e_r
        return (self._raw_logit(diff, total) - self._raw_logit(-diff, total)) / 2.0

    @torch.no_grad()
    def predict_proba(self, blue: torch.Tensor, red: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.forward(blue, red))


@torch.no_grad()
def collect_probs(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[np.ndarray, np.ndarray]:
    model.eval()
    probs: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for blue, red, y in loader:
        p = model.predict_proba(blue.to(device), red.to(device)).cpu().numpy()
        probs.append(p)
        labels.append(y.numpy())
    return np.concatenate(probs), np.concatenate(labels)


def eval_model(model: nn.Module, loader: DataLoader, device: torch.device) -> dict[str, float]:
    probs, labels = collect_probs(model, loader, device)
    return {
        "log_loss": log_loss_np(labels, probs),
        "acc": accuracy_np(labels, probs),
        "ece": ece_np(labels, probs),
    }


def fit_temperature(model: nn.Module, loader: DataLoader, device: torch.device) -> float:
    model.eval()
    all_logits: list[torch.Tensor] = []
    all_labels: list[torch.Tensor] = []
    with torch.no_grad():
        for blue, red, y in loader:
            all_logits.append(model(blue.to(device), red.to(device)).cpu())
            all_labels.append(y)
    logits = torch.cat(all_logits).to(device)
    labels = torch.cat(all_labels).to(device)
    temp = nn.Parameter(torch.ones(1, device=device))
    opt = torch.optim.LBFGS([temp], lr=0.01, max_iter=200)
    criterion = nn.BCEWithLogitsLoss()

    def closure():
        opt.zero_grad()
        loss = criterion(logits / temp.clamp(min=1e-2), labels)
        loss.backward()
        return loss

    opt.step(closure)
    return float(temp.detach().cpu().item())


@torch.no_grad()
def eval_model_temperature(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    temperature: float,
) -> dict[str, float]:
    model.eval()
    logits: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for blue, red, y in loader:
        logits.append(model(blue.to(device), red.to(device)).cpu().numpy())
        labels.append(y.numpy())
    z = np.concatenate(logits)
    y = np.concatenate(labels)
    probs = 1.0 / (1.0 + np.exp(-z / max(temperature, 1e-2)))
    return {
        "log_loss": log_loss_np(y, probs),
        "acc": accuracy_np(y, probs),
        "ece": ece_np(y, probs),
    }


def train_one(
    model: nn.Module,
    train_loader: DataLoader,
    val_loader: DataLoader,
    *,
    device: torch.device,
    epochs: int,
    lr: float,
    weight_decay: float,
    patience: int,
    eval_every: int,
    swap_aug: bool,
) -> tuple[nn.Module, float]:
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.BCEWithLogitsLoss()
    best_val = float("inf")
    best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
    stale = 0

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        n_seen = 0
        for blue, red, y in train_loader:
            blue, red, y = blue.to(device), red.to(device), y.to(device)
            if swap_aug:
                mask = torch.rand(blue.size(0), device=device) < 0.5
                blue_s = torch.where(mask.unsqueeze(1), red, blue)
                red_s = torch.where(mask.unsqueeze(1), blue, red)
                y_s = torch.where(mask, 1.0 - y, y)
                blue, red, y = blue_s, red_s, y_s

            optimizer.zero_grad()
            loss = criterion(model(blue, red), y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * blue.size(0)
            n_seen += blue.size(0)
        scheduler.step()

        if epoch == 1 or epoch % eval_every == 0:
            metrics = eval_model(model, val_loader, device)
            click.echo(
                f"    epoch {epoch:3d} train_loss={total_loss / n_seen:.4f} "
                f"val_log_loss={metrics['log_loss']:.4f} val_acc={metrics['acc']:.4f}"
            )
            if metrics["log_loss"] < best_val:
                best_val = metrics["log_loss"]
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                stale = 0
            else:
                stale += 1
                if stale >= patience:
                    click.echo(f"    early stop at epoch {epoch}")
                    break

    model.load_state_dict(best_state)
    return model, best_val


@click.command()
@click.option("--data", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--ability-json", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--patch-prefix", default="16.10", show_default=True)
@click.option("--out", required=True, type=click.Path(path_type=Path))
@click.option("--embed-dim", default=32, show_default=True)
@click.option("--ability-dim", default=16, show_default=True)
@click.option("--hidden", default=96, show_default=True)
@click.option("--dropout", default=0.2, show_default=True, type=float)
@click.option("--lr", default=2e-3, show_default=True, type=float)
@click.option("--weight-decay", default=5e-3, show_default=True, type=float)
@click.option("--epochs", default=60, show_default=True, type=int)
@click.option("--batch-size", default=512, show_default=True, type=int)
@click.option("--patience", default=5, show_default=True, type=int)
@click.option("--eval-every", default=3, show_default=True, type=int)
@click.option("--seed", default=42, show_default=True, type=int)
def main(
    data: Path,
    ability_json: Path,
    patch_prefix: str,
    out: Path,
    embed_dim: int,
    ability_dim: int,
    hidden: int,
    dropout: float,
    lr: float,
    weight_decay: float,
    epochs: int,
    batch_size: int,
    patience: int,
    eval_every: int,
    seed: int,
) -> None:
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    click.echo(f"[device] {device}")

    feature_map, feature_names = build_ability_feature_map(ability_json)
    splits = load_split_data(data, patch_prefix)
    ability_matrix, missing = make_ability_matrix(splits.champ_to_idx, feature_map)
    click.echo(
        f"[data] train={len(splits.train)} val={len(splits.val)} test={len(splits.test)} "
        f"n_champs={len(splits.champ_to_idx)} blue_base_rate={splits.blue_base_rate:.4f}"
    )
    click.echo(
        f"[ability] features={ability_matrix.shape[1]} missing_champions={missing or 'none'}"
    )

    train_loader = make_loader(splits.train, batch_size, True)
    val_loader = make_loader(splits.val, batch_size, False)
    test_loader = make_loader(splits.test, batch_size, False)

    click.echo("\n[baseline/constant]")
    val_const = {
        "log_loss": log_loss_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "acc": accuracy_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
        "ece": ece_np(splits.val.labels, np.full(len(splits.val), splits.blue_base_rate)),
    }
    test_const = {
        "log_loss": log_loss_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "acc": accuracy_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
        "ece": ece_np(splits.test.labels, np.full(len(splits.test), splits.blue_base_rate)),
    }
    click.echo(f"  val log_loss={val_const['log_loss']:.4f} acc={val_const['acc']:.4f}")

    click.echo("\n[LR baseline]")
    lr_results = lr_train_eval(
        splits.train_lr,
        splits.val_lr,
        splits.test_lr,
        len(splits.champ_to_idx),
    )
    click.echo(
        f"  val log_loss={lr_results['val/log_loss']:.4f} acc={lr_results['val/acc']:.4f} "
        f"test log_loss={lr_results['test/log_loss']:.4f} acc={lr_results['test/acc']:.4f}"
    )

    click.echo("\n[DeepSets embedding-only]")
    base_model = DeepSetsAbility(
        len(splits.champ_to_idx),
        None,
        embed_dim=embed_dim,
        ability_dim=ability_dim,
        hidden=hidden,
        dropout=dropout,
    ).to(device)
    click.echo(f"  params={sum(p.numel() for p in base_model.parameters()):,}")
    base_model, base_best_val = train_one(
        base_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    base_val = eval_model(base_model, val_loader, device)
    base_test = eval_model(base_model, test_loader, device)

    click.echo("\n[DeepSets + ability features]")
    ability_model = DeepSetsAbility(
        len(splits.champ_to_idx),
        ability_matrix,
        embed_dim=embed_dim,
        ability_dim=ability_dim,
        hidden=hidden,
        dropout=dropout,
    ).to(device)
    click.echo(f"  params={sum(p.numel() for p in ability_model.parameters()):,}")
    ability_model, ability_best_val = train_one(
        ability_model,
        train_loader,
        val_loader,
        device=device,
        epochs=epochs,
        lr=lr,
        weight_decay=weight_decay,
        patience=patience,
        eval_every=eval_every,
        swap_aug=True,
    )
    ability_val = eval_model(ability_model, val_loader, device)
    ability_test = eval_model(ability_model, test_loader, device)
    temperature = fit_temperature(ability_model, val_loader, device)
    ability_val_cal = eval_model_temperature(ability_model, val_loader, device, temperature)
    ability_test_cal = eval_model_temperature(ability_model, test_loader, device, temperature)

    rows = [
        ("val", "Constant", val_const),
        ("val", "LR", {"log_loss": lr_results["val/log_loss"], "acc": lr_results["val/acc"], "ece": None}),
        ("val", "DeepSets", base_val),
        ("val", "DeepSets+ability", ability_val),
        ("val", "DeepSets+ability cal", ability_val_cal),
        ("test", "Constant", test_const),
        ("test", "LR", {"log_loss": lr_results["test/log_loss"], "acc": lr_results["test/acc"], "ece": None}),
        ("test", "DeepSets", base_test),
        ("test", "DeepSets+ability", ability_test),
        ("test", "DeepSets+ability cal", ability_test_cal),
    ]
    click.echo("\n[results]")
    headers = ["split", "model", "log_loss", "acc", "ece"]
    table = []
    for split, model_name, metrics in rows:
        ece = metrics["ece"]
        table.append(
            [
                split,
                model_name,
                f"{metrics['log_loss']:.4f}",
                f"{metrics['acc']:.4f}",
                "-" if ece is None else f"{ece:.4f}",
            ]
        )
    widths = [max(len(h), max(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    click.echo("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in table:
        click.echo("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(row))))

    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "data": str(data),
        "ability_json": str(ability_json),
        "patch_prefix": patch_prefix,
        "seed": seed,
        "n_champs": len(splits.champ_to_idx),
        "ability_feature_count": int(ability_matrix.shape[1]),
        "ability_feature_names": feature_names,
        "missing_ability_champions": missing,
        "train_rows": len(splits.train),
        "val_rows": len(splits.val),
        "test_rows": len(splits.test),
        "blue_base_rate": splits.blue_base_rate,
        "base_best_val_log_loss": base_best_val,
        "ability_best_val_log_loss": ability_best_val,
        "ability_temperature": temperature,
        "results": {
            f"{split}/{name}": metrics for split, name, metrics in rows
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    torch.save(
        {
            "base_model": base_model.state_dict(),
            "ability_model": ability_model.state_dict(),
            "ability_temperature": temperature,
            "champ_to_idx": splits.champ_to_idx,
            "ability_feature_names": feature_names,
            "ability_matrix": ability_matrix.cpu(),
        },
        out / "checkpoint.pt",
    )
    click.echo(f"[saved] {out / 'summary.json'}")
    click.echo(f"[saved] {out / 'checkpoint.pt'}")


if __name__ == "__main__":
    main()
