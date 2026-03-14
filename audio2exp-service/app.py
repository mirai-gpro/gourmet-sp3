# -*- coding: utf-8 -*-
"""
Audio2Expression サービス
音声データからARKit 52ブレンドシェイプ表情フレームを生成する

DSPベース推論:
  音声エネルギー + スペクトル特性 → 口形状推定 → ARKit 52ブレンドシェイプ
"""

import base64
import logging
import os
import numpy as np
from scipy.signal import butter, lfilter
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.getenv("PORT", 8080))

# ARKit 52 ブレンドシェイプ名（インデックス固定）
ARKIT_NAMES = [
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight",
    "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthFrownLeft", "mouthFrownRight",
    "mouthFunnel", "mouthLeft",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight",
    "mouthPucker", "mouthRight",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "noseSneerLeft", "noseSneerRight",
    "tongueOut",
]

# ブレンドシェイプ名→インデックスのマップ
_IDX = {name: i for i, name in enumerate(ARKIT_NAMES)}


def _bandpass(data, lowcut, highcut, sr, order=4):
    """バンドパスフィルタ"""
    nyq = 0.5 * sr
    low = max(lowcut / nyq, 0.001)
    high = min(highcut / nyq, 0.999)
    if low >= high:
        return data
    b, a = butter(order, [low, high], btype='band')
    return lfilter(b, a, data)


def _rms_envelope(samples, sr, frame_rate):
    """RMSエンベロープを計算（フレームごと）"""
    hop = int(sr / frame_rate)
    num_frames = max(1, len(samples) // hop)
    envelope = np.zeros(num_frames)
    for i in range(num_frames):
        start = i * hop
        end = min(start + hop, len(samples))
        chunk = samples[start:end]
        if len(chunk) > 0:
            envelope[i] = np.sqrt(np.mean(chunk ** 2))
    return envelope


def _spectral_centroid_per_frame(samples, sr, frame_rate):
    """フレームごとのスペクトル重心を計算"""
    hop = int(sr / frame_rate)
    num_frames = max(1, len(samples) // hop)
    centroids = np.zeros(num_frames)
    for i in range(num_frames):
        start = i * hop
        end = min(start + hop, len(samples))
        chunk = samples[start:end]
        if len(chunk) < 16:
            continue
        fft = np.abs(np.fft.rfft(chunk))
        freqs = np.fft.rfftfreq(len(chunk), 1.0 / sr)
        total = fft.sum()
        if total > 1e-10:
            centroids[i] = np.sum(freqs * fft) / total
    return centroids


def generate_expression_frames(samples, sample_rate, frame_rate=30):
    """
    DSPベースの音声→表情フレーム生成

    音声特徴量:
      - RMSエネルギー → 口の開き度合い (jawOpen, mouthOpen)
      - 音声帯域エネルギー (300-3000Hz) → 発話検出
      - スペクトル重心 → 母音推定（低=あ/お、高=い/え）
      - 高域エネルギー (2000-6000Hz) → 子音/摩擦音検出

    出力: フレームごとのARKit 52ブレンドシェイプ重み
    """
    if len(samples) == 0:
        return [{"weights": [0.0] * 52}]

    # 各帯域のRMSエネルギー
    voice_band = _bandpass(samples, 300, 3000, sample_rate)
    low_band = _bandpass(samples, 100, 500, sample_rate)
    high_band = _bandpass(samples, 2000, 6000, sample_rate)

    env_full = _rms_envelope(samples, sample_rate, frame_rate)
    env_voice = _rms_envelope(voice_band, sample_rate, frame_rate)
    env_low = _rms_envelope(low_band, sample_rate, frame_rate)
    env_high = _rms_envelope(high_band, sample_rate, frame_rate)

    # スペクトル重心
    centroids = _spectral_centroid_per_frame(voice_band, sample_rate, frame_rate)

    num_frames = len(env_full)

    # エネルギーの正規化（95パーセンタイルベース）
    def _normalize(arr):
        p95 = np.percentile(arr, 95) if len(arr) > 0 else 1.0
        if p95 < 1e-6:
            return np.zeros_like(arr)
        return np.clip(arr / p95, 0.0, 1.0)

    env_full_n = _normalize(env_full)
    env_voice_n = _normalize(env_voice)
    env_low_n = _normalize(env_low)
    env_high_n = _normalize(env_high)

    # スペクトル重心を0-1に正規化 (300Hz=0, 3000Hz=1)
    centroid_n = np.clip((centroids - 300) / 2700, 0.0, 1.0)

    # スムージング（急激な変化を抑制）
    alpha = 0.4
    for i in range(1, num_frames):
        env_full_n[i] = alpha * env_full_n[i] + (1 - alpha) * env_full_n[i-1]
        env_voice_n[i] = alpha * env_voice_n[i] + (1 - alpha) * env_voice_n[i-1]
        env_low_n[i] = alpha * env_low_n[i] + (1 - alpha) * env_low_n[i-1]
        env_high_n[i] = alpha * env_high_n[i] + (1 - alpha) * env_high_n[i-1]
        centroid_n[i] = alpha * centroid_n[i] + (1 - alpha) * centroid_n[i-1]

    frames = []
    for i in range(num_frames):
        w = [0.0] * 52
        v = env_voice_n[i]  # 音声エネルギー (0-1)
        lo = env_low_n[i]   # 低域エネルギー
        hi = env_high_n[i]  # 高域エネルギー
        sc = centroid_n[i]  # スペクトル重心 (0=低, 1=高)

        # 発話閾値: 音声エネルギーが低い場合は口を閉じる
        if v < 0.05:
            frames.append({"weights": w})
            continue

        # === 口の開き（エネルギーベース） ===
        jaw_open = v * 0.65
        mouth_open = v * 0.5

        # === 母音推定によるバリエーション ===
        # スペクトル重心が低い → 「あ」「お」（口を大きく丸く）
        # スペクトル重心が高い → 「い」「え」（口を横に引く）
        if sc < 0.35:
            # 「あ」「お」系: 口を丸く大きく
            jaw_open *= 1.2
            mouth_funnel = v * 0.25
            mouth_pucker = v * 0.1 if lo > 0.3 else 0.0  # 「お」
            mouth_stretch_l = 0.0
            mouth_stretch_r = 0.0
        elif sc > 0.6:
            # 「い」「え」系: 口を横に引く
            jaw_open *= 0.7
            mouth_funnel = 0.0
            mouth_pucker = 0.0
            mouth_stretch_l = v * 0.3
            mouth_stretch_r = v * 0.3
        else:
            # 「う」系 or 遷移: 口をすぼめる
            jaw_open *= 0.85
            mouth_funnel = v * 0.15
            mouth_pucker = v * 0.2
            mouth_stretch_l = v * 0.05
            mouth_stretch_r = v * 0.05

        # === 子音/摩擦音（高域） ===
        if hi > 0.3:
            mouth_funnel = max(mouth_funnel, hi * 0.2)

        # 下唇の動き
        mouth_lower_l = v * 0.25
        mouth_lower_r = v * 0.25
        mouth_upper_l = v * 0.1
        mouth_upper_r = v * 0.1

        # 微笑み（常に軽く）
        smile = 0.08

        # 値をクランプして設定
        w[_IDX["jawOpen"]] = float(np.clip(jaw_open, 0, 1))
        w[_IDX["mouthOpen"]] = float(np.clip(mouth_open, 0, 1))  # jawOpen - mouthClose的な使い方もある
        w[_IDX["mouthFunnel"]] = float(np.clip(mouth_funnel, 0, 1))
        w[_IDX["mouthPucker"]] = float(np.clip(mouth_pucker, 0, 1))
        w[_IDX["mouthStretchLeft"]] = float(np.clip(mouth_stretch_l, 0, 1))
        w[_IDX["mouthStretchRight"]] = float(np.clip(mouth_stretch_r, 0, 1))
        w[_IDX["mouthLowerDownLeft"]] = float(np.clip(mouth_lower_l, 0, 1))
        w[_IDX["mouthLowerDownRight"]] = float(np.clip(mouth_lower_r, 0, 1))
        w[_IDX["mouthUpperUpLeft"]] = float(np.clip(mouth_upper_l, 0, 1))
        w[_IDX["mouthUpperUpRight"]] = float(np.clip(mouth_upper_r, 0, 1))
        w[_IDX["mouthSmileLeft"]] = smile
        w[_IDX["mouthSmileRight"]] = smile

        frames.append({"weights": w})

    logger.debug(f"[A2E] {num_frames}フレーム生成, "
                 f"avg_voice={env_voice_n.mean():.3f}, avg_centroid={centroid_n.mean():.3f}")
    return frames


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/audio2expression", methods=["POST"])
def audio2expression():
    """
    音声データからARKit 52ブレンドシェイプ表情フレームを生成

    入力:
        audio_base64: base64エンコードされた音声データ
        session_id: セッションID
        audio_format: "mp3" | "pcm_24000_16bit_mono" (デフォルト: "mp3")
        is_start: bool - セグメント開始フラグ
        is_final: bool - セグメント終了フラグ

    出力:
        names: string[52] - ARKit ブレンドシェイプ名
        frames: [{weights: float[52]}] - フレームごとの重み
        frame_rate: int - フレームレート（通常30）
    """
    data = request.get_json()
    if not data or "audio_base64" not in data:
        return jsonify({"error": "audio_base64 is required"}), 400

    audio_base64 = data["audio_base64"]
    session_id = data.get("session_id", "unknown")
    audio_format = data.get("audio_format", "mp3")

    try:
        audio_data = base64.b64decode(audio_base64)

        if audio_format == "pcm_24000_16bit_mono":
            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            sample_rate = 24000
        else:
            from io import BytesIO
            from pydub import AudioSegment

            audio_segment = AudioSegment.from_file(BytesIO(audio_data), format="mp3")
            audio_segment = audio_segment.set_channels(1).set_frame_rate(24000).set_sample_width(2)
            samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float32) / 32768.0
            sample_rate = 24000

        duration_sec = len(samples) / sample_rate
        logger.info(f"[A2E] 入力: {audio_format}, {duration_sec:.2f}秒, session={session_id}")

        frame_rate = 30
        frames = generate_expression_frames(samples, sample_rate, frame_rate)

        result = {
            "names": ARKIT_NAMES,
            "frames": frames,
            "frame_rate": frame_rate,
        }

        logger.info(f"[A2E] 生成完了: {len(frames)}フレーム, session={session_id}")
        return jsonify(result)

    except Exception as e:
        logger.error(f"[A2E] 処理エラー: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
