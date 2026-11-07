// ArkTS 侧 NAPI 类型声明（契约，**不得修改**）。
// 缓冲区均为 RGBA8888、行优先、长度 >= width*height*4；函数返回处理后的新缓冲区。

export const grayscale: (buffer: ArrayBuffer, width: number, height: number) => ArrayBuffer;
export const adjustBrightness: (buffer: ArrayBuffer, width: number, height: number, delta: number) => ArrayBuffer;
export const adjustContrast: (buffer: ArrayBuffer, width: number, height: number, factor: number) => ArrayBuffer;
export const boxBlur3: (buffer: ArrayBuffer, width: number, height: number) => ArrayBuffer;
export const rotate90: (buffer: ArrayBuffer, width: number, height: number) => ArrayBuffer;
