/*
 * 文件夹选择（浏览器侧，取代「后端弹系统对话框」）。
 *
 * ## 为什么必须换
 *
 * 原实现让**服务器进程**弹 Windows `FolderBrowserDialog`，把 `D:\...` 返回给前端。
 * 那在桌面单机形态成立，在服务器形态**必然坏**：
 *
 * 1. 服务器没有桌面会话 ⇒ 对话框弹不出（或卡满 180 秒超时）；
 * 2. 就算弹出，那是**机房那台机器**的对话框，用户看不到；
 * 3. 返回的路径是服务器本地路径，而用户的数据包在他自己的电脑上。
 *
 * 用户口径「桌面端不再使用 / 部署到服务器」⇒ 改成浏览器选目录 + 上传。
 *
 * ## 关键点：`webkitRelativePath`
 *
 * `input[webkitdirectory]` 选中的每个 `File` 都带 `webkitRelativePath`
 * （形如 `我的数据包/主图/1.jpg`），后端**靠这个保留目录结构**做区域分区
 * （`staging.stage_package` 按相对路径落盘）。
 * ⇒ 上传时必须用 `form.append("files", blob, relPath)`，不能用 `file.name`
 *   （那样所有文件都会挤在根目录，区域信息全丢）。
 *
 * ⚠️ `webkitdirectory` 是 Chrome/Edge/Safari 私有属性（React 需要写成
 * `{...{ webkitdirectory: "" }}`，直接 `webkitdirectory=""` 会被 TS 拒）。
 * Firefox 支持较晚（113+）。本系统是内网管理后台，限定 Chromium 系即可。
 */

/** 一个待上传的文件：`relPath` 含相对目录（后端据此分区）。 */
export interface PickedFile {
  relPath: string;
  blob: File;
}

export interface PickedFolder {
  /** 顶层目录名（用作数据包名） */
  folderName: string;
  files: PickedFile[];
}

/** 从 `<input type="file" webkitdirectory>` 的 FileList 里提取「相对路径 + 文件」。 */
export function collectFolder(
  fileList: FileList | File[] | null | undefined,
): PickedFolder | null {
  const files = fileList ? Array.from(fileList) : [];
  if (files.length === 0) return null;

  const picked: PickedFile[] = [];
  let folderName = "";
  for (const file of files) {
    //: `webkitRelativePath` 形如 `包名/子目录/文件.jpg`；非 webkitdirectory 选择时为空。
    const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
    if (!folderName) {
      const head = rel.split("/")[0];
      folderName = head || "";
    }
    picked.push({ relPath: rel, blob: file });
  }
  return { folderName: folderName || "数据包", files: picked };
}

/**
 * 打开系统文件夹选择框并收集文件（`<input>` 由调用方渲染，这里只负责点击与解析）。
 *
 * 之所以不在本函数里动态创建 `<input>`：部分浏览器要求点击必须发生在
 * **用户手势的同步栈**里，异步创建再 `click()` 会被拦（表现是「点了没反应」）。
 * ⇒ 由 React 渲染一个隐藏 input + `ref.current?.click()`。
 */
export function readFolderInput(input: HTMLInputElement | null): PickedFolder | null {
  return collectFolder(input?.files);
}
