# 机器人宽屏模板（本地草稿）

这份模板使用普通模板协议，供原企业机器人使用；不是 A2UI JSON。
目前完成的是本地模板、机器人接入和浏览器排版稿。尚未在钉钉后台导入、保存或发布，也没有客户端宽屏验证。

## 文件

- `wide-answer.template.json`：基于官方流式 AI 卡片导出格式，包含 `editorData`、`widgetInfo`、`type`、`mode`。编辑器源结构和 XML 布局同时调整。
- `wide-answer.sample.json`：仅含合成排版测试数据，不包含检索材料或真实业务来源。
- `wide-answer.preview.html`：可直接打开的本地浏览器效果稿，支持宽屏/窄屏切换。测试正文与 sample 一致。蓝色背景用于视觉对照，正式模板保留平台原生背景；该页面不调用钉钉渲染器，不能用于证明客户端效果。

## 模板行为

- 声明对象变量 `config` 及布尔子属性 `autoLayout`；同时声明官方投放示例中的顶层布尔变量 `autoLayout`。
- 输入中、完成、失败三种状态，均绑定 `msgTitle` 和 Markdown 正文 `msgContent`，正文和容器填满父级宽度。
- 输入态的 `msgContent` 开启流式；其他状态复用正文，状态编号保持 `1/2/3/5`。
- 关闭模板原有的 480 高度自动折叠，避免长答案和来源默认隐藏。
- 移除没有接入业务回调的反馈/生成详情组件，关闭中止生成按钮；保留失败正文和程序中的文本回退。
- 模板不会重写答案或生成来源。编号和空行沿用回答提示词，真实来源沿用检索记录生成逻辑。

## 接入顺序（尚未执行）

1. 在卡片平台为原机器人“小钉”创建“消息卡片 / AI 卡片”模板，导入 `wide-answer.template.json`。核对编辑器显示的变量、输入态和完成态，重新保存、编译模板；本地 XML 检查不能替代平台编译。
2. 用 sample 中的数据在平台预览。检查桌面宽窗口、窄窗口和手机端，确认完整来源可见；卡片平台预览也不能替代实际 IM 场域验证。
3. 发布后，将实际模板 ID 填入运行环境的 `DINGTALK_AI_CARD_TEMPLATE_ID`。不要将本地文件路径或示例 ID 当作模板 ID。
4. 本人单聊验证需用新 `outTrackId`，在原机器人下检查投放、连续更新、完成/失败状态及实际宽度。接口成功不能作为宽屏验收。
5. 用户授权部署后再更新服务。当前未修改真实环境文件、未重建/重启 Docker、未创建后台模板、未发送验证卡。

ID 留空时继续使用 SDK 公共模板，因此本地修改本身不会将线上卡片变宽。公共模板未读取到布局声明，本轮没有认定它可编辑或支持宽屏。增加 `autoLayout` 参数只是请求宽屏，最终受模板与客户端支持情况影响；不承诺复制 A2UI 预览的蓝色背景或完全相同宽度。

## 官方依据

- [配置宽屏卡片](https://open.dingtalk.com/document/development/configure-widescreen-cards)：模板声明 `config.autoLayout`，投放示例传 `autoLayout: "true"`。
- [API 卡片数据填写](https://open.dingtalk.com/document/orgapp/instructions-for-filling-in-api-card-data)：对象变量需要序列化为 JSON 字符串，因此代码还传入 `config: "{\"autoLayout\": true}"`。
- [打字机效果流式 AI 卡片](https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card)：关联机器人应用、Markdown 流式开关和变量绑定。
- [官方示例源文件](https://github.com/open-dingtalk/dingtalk-card-examples/blob/main/examples/打字机效果流式更新%20AI%20卡片/打字机效果流式更新%20AI%20卡片.json)：本地模板的原始导出格式及组件基础。

本地验证覆盖 JSON/XML 结构、机器人与模板的变量及状态契约、请求参数、流式更新、错误处理和文本回退。未做后台导入校验、原生客户端渲染或真实收发验证。
