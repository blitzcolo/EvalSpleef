# 图片滤镜 Demo（HarmonyOS）

一个单页应用：选一张本地图片，点「灰度 / 提亮 / 增强对比 / 模糊 / 旋转 90°」按钮后，
预览区立刻显示处理后的效果。图像处理逻辑要求放在 C++ 侧（NAPI 插件），ArkTS 只负责界面与调用。

## 目录

    entry/src/main/ets/pages/Index.ets            # 页面（UI 骨架已给，缺调用与状态处理）
    entry/src/main/cpp/image_ops.h                # C++ 处理接口契约（**不得修改**）
    entry/src/main/cpp/image_ops.cpp              # 处理实现（当前是空壳）
    entry/src/main/cpp/napi_init.cpp              # NAPI 绑定（当前只注册了一个占位函数）
    entry/src/main/cpp/types/libimageops/index.d.ts  # ArkTS 侧类型声明（契约，**不得修改**）
    entry/src/main/module.json5                   # 模块配置（缺权限声明）
    build-profile.json5                           # 构建配置（签名占位）

## 约束

- ArkTS 只调 NAPI 暴露的同步接口；不要在 ArkTS 里用其他图像库绕过 C++。
- C++ 实现只能改 `image_ops.cpp`（可新增自己的 .cpp/.h，但必须在 CMakeLists.txt 里登记）。
- C++ 代码要能被独立编译成 host 可执行文件做数值自测（不要依赖 NAPI 运行时类型）。
- 构建/签名/装机命令见题面；签名材料由评测平台预置，不要自己申请账号。

## 构建、签名、装机、看日志

命令行工具（hvigorw / ohpm / hdc）的 PATH 由平台预置，签名材料在 /opt/harmony-signing。

~~~bash
# 0) 依赖（首次或改过 oh-package.json5 后）
ohpm install --all

# 1) 构建 debug HAP（产物在 entry/build/default/outputs/default/entry-default-signed.hap）
hvigorw clean --no-daemon
hvigorw assembleHap --mode module -p product=default -p buildMode=debug --no-daemon

# 2) 装机（设备由平台挂好，hdc 已连）
hdc list targets
hdc install -r entry/build/default/outputs/default/entry-default-signed.hap

# 3) 启动并看日志
hdc shell aa start -a EntryAbility -b com.example.imagefilters
hdc shell hilog -T imagefilters -t app
~~~

签名配置已写在 build-profile.json5，证书/Profile 由平台注入；若构建报签名错误，先确认 /opt/harmony-signing 已挂载，
不要把密码或证书写进代码或提交到工作区。
