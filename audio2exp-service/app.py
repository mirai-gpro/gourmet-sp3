# -*- coding: utf-8 -*-
"""
Audio2Expression サービス
音声データからARKit 52ブレンドシェイプ表情フレームを生成する

【移植元】C:\Users\hamad\audio2exp-service
【拡張】PCM 24kHz 16bit mono 入力対応（LiveAPIストリーミング用）
"""

import base64
import logging
import os
import numpy as np
from flask import Flask, request, jsonify
from flask_cors import CORS

# audio2expression コア処理（移植元の推論ロジック）
# TODO: 移植元から audio2expression.py をコピーして配置する
# from audio2expression import generate_expression_frames

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PORT = int(os.getenv("PORT", 8080))


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
    is_start = data.get("is_start", True)
    is_final = data.get("is_final", True)

    try:
        audio_data = base64.b64decode(audio_base64)

        if audio_format == "pcm_24000_16bit_mono":
            # ★ PCM入力: LiveAPI PCMストリーミング用
            # 24kHz 16bit mono PCM → numpy float32 array
            samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            sample_rate = 24000
            duration_sec = len(samples) / sample_rate
            logger.info(
                f"[A2E] PCM入力: {len(samples)}サンプル, {duration_sec:.2f}秒, session={session_id}"
            )
        else:
            # MP3入力: 既存のpydub変換パス
            # TODO: pydubでMP3→numpy変換
            from io import BytesIO
            from pydub import AudioSegment

            audio_segment = AudioSegment.from_file(BytesIO(audio_data), format="mp3")
            audio_segment = audio_segment.set_channels(1).set_frame_rate(24000).set_sample_width(2)
            samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float32) / 32768.0
            sample_rate = 24000
            duration_sec = len(samples) / sample_rate
            logger.info(
                f"[A2E] MP3入力: {duration_sec:.2f}秒, session={session_id}"
            )

        # TODO: 実際のA2E推論処理をここに実装
        # frames = generate_expression_frames(samples, sample_rate)
        #
        # 暫定: ダミーフレームを生成（移植元のモデルが配置されるまで）
        frame_rate = 30
        num_frames = max(1, int(duration_sec * frame_rate))
        frames = [{"weights": [0.0] * 52} for _ in range(num_frames)]

        # ARKit 52 ブレンドシェイプ名
        arkit_names = [
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

        result = {
            "names": arkit_names,
            "frames": frames,
            "frame_rate": frame_rate,
        }

        logger.info(f"[A2E] 生成完了: {num_frames}フレーム, session={session_id}")
        return jsonify(result)

    except Exception as e:
        logger.error(f"[A2E] 処理エラー: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
