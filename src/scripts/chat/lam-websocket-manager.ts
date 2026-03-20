// src/scripts/chat/lam-websocket-manager.ts
/**
 * LAM WebSocket Manager - Gaussian Splat アバターの管理
 *
 * LAM_gpro より移植・改修:
 * - gaussian-splat-renderer-for-lam パッケージの公式APIを使用
 * - GaussianSplatRenderer.getInstance(div, assetPath, callbacks) パターン
 * - ARKit 52ブレンドシェイプのコールバック方式適用
 * - LiveAudioManager との同期連携
 *
 * 仕様書08 セクション6:
 * External TTS Player モードから LiveAudioManager モードへ変更
 */

import * as GaussianSplats3D from 'gaussian-splat-renderer-for-lam';
import type { ExpressionFrame } from './live-audio-manager';

export interface LAMConfig {
    containerElement: HTMLDivElement;   // レンダラーを埋め込むdiv要素
    modelUrl: string;                  // .zip モデルURL
}

// ARKit 52 ブレンドシェイプ名（標準順序）
export const ARKIT_BLENDSHAPE_NAMES = [
    'eyeBlinkLeft', 'eyeLookDownLeft', 'eyeLookInLeft', 'eyeLookOutLeft', 'eyeLookUpLeft',
    'eyeSquintLeft', 'eyeWideLeft', 'eyeBlinkRight', 'eyeLookDownRight', 'eyeLookInRight',
    'eyeLookOutRight', 'eyeLookUpRight', 'eyeSquintRight', 'eyeWideRight',
    'jawForward', 'jawLeft', 'jawRight', 'jawOpen',
    'mouthClose', 'mouthFunnel', 'mouthPucker', 'mouthLeft', 'mouthRight',
    'mouthSmileLeft', 'mouthSmileRight', 'mouthFrownLeft', 'mouthFrownRight',
    'mouthDimpleLeft', 'mouthDimpleRight', 'mouthStretchLeft', 'mouthStretchRight',
    'mouthRollLower', 'mouthRollUpper', 'mouthShrugLower', 'mouthShrugUpper',
    'mouthPressLeft', 'mouthPressRight', 'mouthLowerDownLeft', 'mouthLowerDownRight',
    'mouthUpperUpLeft', 'mouthUpperUpRight',
    'browDownLeft', 'browDownRight', 'browInnerUp', 'browOuterUpLeft', 'browOuterUpRight',
    'cheekPuff', 'cheekSquintLeft', 'cheekSquintRight',
    'noseSneerLeft', 'noseSneerRight',
    'tongueOut'
];

export class LAMWebSocketManager {
    private renderer: GaussianSplats3D.GaussianSplatRenderer | null = null;
    private isModelLoaded: boolean = false;
    private _exprDebugCounter: number = 0;  // デバッグログ間引き用

    // 現在適用中の expression フレーム
    private currentExpression: ExpressionFrame | null = null;

    // チャット状態（Idle / Listening / Thinking / Responding）
    private chatState: string = 'Idle';

    // ブレンドシェイプ名マッピング
    private expressionNames: string[] = ARKIT_BLENDSHAPE_NAMES;

    /**
     * 初期化: Gaussian Splat レンダラーをセットアップ
     * 公式API: GaussianSplatRenderer.getInstance(div, assetPath, callbacks)
     */
    async initialize(config: LAMConfig): Promise<void> {
        try {
            this.renderer = await GaussianSplats3D.GaussianSplatRenderer.getInstance(
                config.containerElement,
                config.modelUrl,
                {
                    getChatState: () => this.chatState,
                    getExpressionData: () => this._getExpressionData(),
                    backgroundColor: '0x000000',
                    alpha: 0.0,
                }
            );

            // カメラ位置を調整してアバターの顔サイズ・位置を制御
            // デフォルト: x=0, y=1.8, z=1
            if (this.renderer.viewer && this.renderer.viewer.camera) {
                const camera = this.renderer.viewer.camera;
                camera.position.z = 0.4;    // 近づけて顔を大きく
                camera.position.y = 1.73;   // カメラの高さ（やや上から見下ろして鼻の穴を目立たなく）

                // 注視点（controls.target）を顔〜首の高さに合わせる
                const controls = this.renderer.viewer.controls;
                if (controls) {
                    controls.target.set(0, 1.62, 0);  // 顔+首が見える高さを注視
                    controls.update();
                }

                camera.updateProjectionMatrix();
                console.log('[LAMWebSocketManager] カメラ位置調整: y=', camera.position.y, 'z=', camera.position.z, 'target.y=', controls?.target?.y);
            }

            this.isModelLoaded = true;
            console.log('[LAMWebSocketManager] モデルロード完了');
        } catch (error) {
            console.error('[LAMWebSocketManager] 初期化エラー:', error);
            this.isModelLoaded = false;
        }
    }

    /**
     * コールバック: 現在のexpressionデータをブレンドシェイプmapとして返す
     */
    private _getExpressionData(): Record<string, number> {
        const result: Record<string, number> = {};

        if (!this.currentExpression) {
            // 静止状態: 全て0
            for (const name of this.expressionNames) {
                result[name] = 0;
            }
            return result;
        }

        const values = this.currentExpression.values;
        for (let i = 0; i < Math.min(values.length, this.expressionNames.length); i++) {
            result[this.expressionNames[i]] = values[i];
        }

        // === STEP1: 日本語口形補正 ===
        // 案B: 静的スケーリング（全体適用）
        const JP_SCALING: Record<string, number> = {
            'mouthStretchLeft':  1.3,
            'mouthStretchRight': 1.3,
            'jawOpen':           0.85,
            'mouthFunnel':       1.1,
            'mouthPucker':       1.1,
        };
        for (const [name, scale] of Object.entries(JP_SCALING)) {
            if (result[name] !== undefined) {
                result[name] = Math.min(1.0, result[name] * scale);
            }
        }

        // 案A: ブレンドシェイプ間の関係制約（イ/エ系でjawOpen追加抑制）
        const stretchL = result['mouthStretchLeft'] ?? 0;
        const stretchR = result['mouthStretchRight'] ?? 0;
        const avgStretch = (stretchL + stretchR) / 2;
        if (avgStretch > 0.2) {
            const suppressionFactor = 1.0 - avgStretch * 0.5;
            result['jawOpen'] *= Math.max(0.3, suppressionFactor);
        }

        // デバッグ: 120フレームごと（約2秒）にログ出力
        this._exprDebugCounter++;
        if (this._exprDebugCounter % 120 === 0) {
            const jawOpen = result['jawOpen'] ?? 'N/A';
            const mouthOpen = result['mouthOpen'] ?? 'N/A';
            const nonZero = Object.entries(result).filter(([, v]) => v > 0.01).length;
            console.log(
                `[LAM ExprData] jawOpen=${typeof jawOpen === 'number' ? jawOpen.toFixed(3) : jawOpen}, ` +
                `mouthOpen=${typeof mouthOpen === 'number' ? mouthOpen.toFixed(3) : mouthOpen}, ` +
                `nonZero=${nonZero}/${this.expressionNames.length}, ` +
                `valuesLen=${values.length}, namesLen=${this.expressionNames.length}`
            );
        }

        return result;
    }

    /**
     * expression フレームを更新
     */
    updateExpression(frame: ExpressionFrame | null): void {
        this.currentExpression = frame;
    }

    /**
     * チャット状態を更新
     */
    setChatState(state: string): void {
        this.chatState = state;
    }

    /**
     * ブレンドシェイプ名リストを設定
     */
    setExpressionNames(names: string[]): void {
        if (names && names.length > 0) {
            this.expressionNames = names;
        }
    }

    /**
     * モデルがロード済みかどうか
     */
    isLoaded(): boolean {
        return this.isModelLoaded;
    }

    /**
     * expression バッファをクリア（リセット）
     */
    resetExpression(): void {
        this.currentExpression = null;
        this.chatState = 'Idle';
    }

    /**
     * クリーンアップ
     */
    dispose(): void {
        this.renderer = null;
        this.isModelLoaded = false;
        this.currentExpression = null;
    }
}
