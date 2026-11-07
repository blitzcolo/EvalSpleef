import { image } from '@kit.ImageKit';
import { photoAccessHelper } from '@kit.MediaLibraryKit';
import { promptAction } from '@kit.ArkUI';
import imageOps from 'libimageops.so';

@Entry
@Component
struct Index {
  @State preview: image.PixelMap | undefined = undefined;
  @State source: image.PixelMap | undefined = undefined;
  @State width: number = 0;
  @State height: number = 0;
  @State status: string = '请选择一张图片';

  private async pickImage(): Promise<void> {
    const picker = new photoAccessHelper.PhotoViewPicker();
    const result = await picker.select({
      MIMEType: photoAccessHelper.PhotoViewMIMETypes.IMAGE_TYPE,
      maxSelectNumber: 1
    });
    if (result.photoUris.length === 0) {
      return;
    }
    const file = await fsOpenReadOnly(result.photoUris[0]);
    const source = await image.createImageSource(file);
    const pixmap = await source.createPixelMap();
    this.source = pixmap;
    this.preview = pixmap;
    const info = await pixmap.getImageInfo();
    this.width = info.size.width;
    this.height = info.size.height;
    this.status = '已载入 ' + this.width + 'x' + this.height;
  }

  // TODO: 从源图取出 RGBA 缓冲区（pixmap.readPixelsToBuffer），调用 imageOps 的对应函数，
  // 再用 image.createPixelMap 从返回的 ArrayBuffer 生成新 PixelMap 并赋给 this.preview。
  private applyGrayscale(): void {
    this.status = '灰度：待实现';
  }

  private applyBrightness(): void {
    this.status = '提亮：待实现';
  }

  private applyContrast(): void {
    this.status = '对比度：待实现';
  }

  private applyBlur(): void {
    this.status = '模糊：待实现';
  }

  private applyRotate(): void {
    this.status = '旋转：待实现';
  }

  build() {
    Column({ space: 12 }) {
      Text('图片滤镜').fontSize(24).fontWeight(FontWeight.Bold)
      Text(this.status).fontSize(14).fontColor('#666666')

      Image(this.preview)
        .width('100%')
        .height(320)
        .objectFit(ImageFit.Contain)
        .backgroundColor('#F2F2F2')

      Row({ space: 8 }) {
        Button('选择图片').onClick(() => { this.pickImage(); })
        Button('灰度').enabled(this.source !== undefined).onClick(() => { this.applyGrayscale(); })
        Button('提亮').enabled(this.source !== undefined).onClick(() => { this.applyBrightness(); })
      }
      Row({ space: 8 }) {
        Button('增强对比').enabled(this.source !== undefined).onClick(() => { this.applyContrast(); })
        Button('模糊').enabled(this.source !== undefined).onClick(() => { this.applyBlur(); })
        Button('旋转90°').enabled(this.source !== undefined).onClick(() => { this.applyRotate(); })
      }
      if (this.preview !== undefined) {
        Button('恢复原图').onClick(() => { this.preview = this.source; })
      }
    }
    .padding(16)
    .width('100%')
  }
}

// 最小文件读取辅助：把沙箱 URI 读成 ArrayBuffer（HarmonyOS 用 fs.open/read）。
import { fileIo as fs } from '@kit.CoreFileKit';
async function fsOpenReadOnly(uri: string): Promise<ArrayBuffer> {
  const file = fs.openSync(uri, fs.OpenMode.READ_ONLY);
  try {
    const stat = fs.statSync(file.fd);
    const buffer = new ArrayBuffer(stat.size);
    fs.readSync(file.fd, buffer);
    return buffer;
  } finally {
    fs.closeSync(file);
  }
}
