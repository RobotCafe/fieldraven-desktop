#!/usr/bin/env bash
# GlueMap WSL2 setup script
# Run inside WSL2: bash /mnt/c/Users/DenmanNic/Projects/FieldRaven_desktop/scripts/setup_gluemap_wsl.sh
set -euo pipefail

GLUEMAP_DIR="$HOME/gluemap"
CONDA_ENV="gluemap"

echo ""
echo "========================================"
echo "  GlueMap WSL2 Setup"
echo "========================================"
echo ""

# ── 1. micromamba ─────────────────────────────────────────────────
echo "=== [1/6] micromamba ==="
# Always ensure ~/.local/bin is in PATH (where micromamba installs)
export PATH="$HOME/.local/bin:$PATH"
if ! command -v micromamba &>/dev/null; then
    echo "Installing micromamba..."
    mkdir -p "$HOME/.local/bin"
    curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C "$HOME/.local/bin" --strip-components=1 bin/micromamba
    echo "micromamba installed at $HOME/.local/bin/micromamba"
fi
echo "micromamba: $(micromamba --version)"
eval "$(micromamba shell hook --shell bash)"

# ── 2. Clone repo ─────────────────────────────────────────────────
echo ""
echo "=== [2/6] Clone gluemap ==="
if [ ! -d "$GLUEMAP_DIR" ]; then
    git clone https://github.com/colmap/gluemap.git "$GLUEMAP_DIR"
    cd "$GLUEMAP_DIR"
    git submodule update --init --recursive
else
    echo "Already cloned at $GLUEMAP_DIR"
    cd "$GLUEMAP_DIR"
    git pull --ff-only || true
fi

# ── 3. Conda environment ───────────────────────────────────────────
echo ""
echo "=== [3/6] Create conda environment '$CONDA_ENV' ==="
if micromamba env list 2>/dev/null | grep -q "^$CONDA_ENV "; then
    echo "Environment '$CONDA_ENV' already exists, skipping create"
else
    micromamba create -n "$CONDA_ENV" python=3.11 -c conda-forge -y
fi

# ── 4. Install packages ────────────────────────────────────────────
echo ""
echo "=== [4/6] Install conda packages (this takes a few minutes) ==="
micromamba install -n "$CONDA_ENV" -c conda-forge -y \
    "eigen=3.4.0" \
    "ceres-solver=2.2.0" \
    "metis=5.1.0" \
    "boost=1.85.0" \
    "libstdcxx-ng=15.2.0" \
    "pytorch-gpu=2.4.1" \
    "torchvision=0.19.1" \
    "cuda-version=12.4" \
    compilers \
    huggingface_hub \
    wget

# ── 5. Build gluemap ──────────────────────────────────────────────
echo ""
echo "=== [5/6] Build and install gluemap ==="
cd "$GLUEMAP_DIR"
micromamba run -n "$CONDA_ENV" bash -c \
    "CMAKE_PREFIX_PATH=\$CONDA_PREFIX pip install -e ."

# ── 6. Download checkpoints ────────────────────────────────────────
echo ""
echo "=== [6/6] Download model checkpoints ==="
mkdir -p "$GLUEMAP_DIR/checkpoints"
cd "$GLUEMAP_DIR/checkpoints"

# SALAD retrieval (~100 MB)
if [ ! -f "dino_salad.ckpt" ]; then
    echo "Downloading SALAD retrieval model..."
    wget -q --show-progress -O dino_salad.ckpt \
        https://github.com/serizba/salad/releases/download/v1.0.0/dino_salad.ckpt
else
    echo "dino_salad.ckpt already present"
fi

# VGGSfM tracker — used for track snapping in refinement stage (~300 MB)
if [ ! -f "vggsfm_v2_0_0_track_predictor.bin" ]; then
    echo "Downloading VGGSfM track predictor..."
    micromamba run -n "$CONDA_ENV" python -c "
from huggingface_hub import hf_hub_download
import os
hf_hub_download('facebook/VGGSfM', 'vggsfm_v2_tracker.pt', local_dir='.')
os.rename('vggsfm_v2_tracker.pt', 'vggsfm_v2_0_0_track_predictor.bin')
print('Done: vggsfm_v2_0_0_track_predictor.bin')
"
else
    echo "vggsfm_v2_0_0_track_predictor.bin already present"
fi

# Pi3 feedforward backbone (~1.5 GB)
if [ ! -f "pi3.safetensors" ]; then
    echo "Downloading Pi3 backbone (~1.5 GB)..."
    micromamba run -n "$CONDA_ENV" python -c "
from huggingface_hub import hf_hub_download
import os
hf_hub_download('yyfz233/Pi3', 'model.safetensors', local_dir='.')
os.rename('model.safetensors', 'pi3.safetensors')
print('Done: pi3.safetensors')
"
else
    echo "pi3.safetensors already present"
fi

# Doppelgangers++ — covisibility estimation (optional, ~500 MB)
# Set skip_doppelgangers: true in config to skip this at runtime
if [ ! -f "checkpoint-dg+visym.pth" ]; then
    echo "Downloading Doppelgangers++..."
    micromamba run -n "$CONDA_ENV" python -c "
from huggingface_hub import hf_hub_download
hf_hub_download('doppelgangers25/doppelgangers_plusplus',
                'checkpoint-dg+visym.pth', local_dir='.')
print('Done: checkpoint-dg+visym.pth')
"
else
    echo "checkpoint-dg+visym.pth already present"
fi

echo ""
echo "========================================"
echo "  Setup complete!"
echo "========================================"
echo ""
echo "Checkpoints in: $GLUEMAP_DIR/checkpoints/"
ls -lh "$GLUEMAP_DIR/checkpoints/"
echo ""
echo "Test run:"
echo "  micromamba run -n gluemap gluemap-demo \\"
echo "    --config $GLUEMAP_DIR/configs/example.yaml \\"
echo "    --images_path /mnt/c/FieldRaven/<project>/images \\"
echo "    --intrinsics_mode PER_FOLDER \\"
echo "    --write_path /tmp/gluemap_test"
