#   AstrBot今日运势图生成插件

## 使用说明

**输入 /jrys 或者（/今日运势 ，/运势）生成该用户的运势图**

**jrys 今日运势 运势 等关键词也可以触发， 可在插件配置里面找到 启用关键字触发 (jrys_keyword_enabled) 选择关闭（默认开启）**

## 背景图片源

背景图由插件目录下的 **backgrounds.json** 统一管理，支持四种图源格式：

```json
{
  "miku": ["https://直链1.jpg", "https://直链2.jpg"],
  "single": "https://直链.jpg",
  "lolicon_api": {
    "type": "api",
    "url": "https://api.lolicon.app/setu/v2?r18=0&size=regular",
    "token": "data.0.urls.regular"
  },
  "local_mix": { "type": "object", "sources": ["https://...", {"url": "https://..."}] }
}
```

- `api` 类型会先请求接口，再按 `token` 路径（如 `data.0.urls.regular`）从返回的 JSON 里取图片地址；若接口直接返回图片，加 `"expected": "image"`
- 配置面板里可用 **忽略的图片源 (ignored_sources)** 禁用某些图源，用 **图源权重 (source_weights)** 控制抽中概率（格式 `ba:2.0`）
- 预缓存功能会下载所有静态直链（api 类型跳过）

## 平台支持

- **OneBot v11（aiocqhttp / NapCat / Lagrange 等）**：完整支持，头像走 QQ 头像直链
- **QQ 官方机器人（qqofficial）**：频道消息会直接获取用户头像；群聊 / C2C 私聊官方接口不提供头像，会自动生成「彩色圆形 + 用户名首字」的默认头像
- **Telegram**：通过 Bot API `getUserProfilePhotos` 获取用户头像，用户没设置头像时同样回退默认头像
- 其他平台：按 QQ 头像直链尝试，失败自动回退默认头像

> 默认配置下海报**不再绘制头像**，发送结果时会先 @ 用户再发图（配置项 `show_avatar` 可重新开启图上头像）。

生成图的风格照着 [https://github.com/shangxueink/koishi-shangxue-apps/tree/main/plugins/jrys-prpr](https://github.com/shangxueink/koishi-shangxue-apps/tree/main/plugins/jrys-prpr)
这个项目的写的 因为我挺喜欢这个作者的审美的


## 效果图展示

![效果图展示](./README.assets/1.jpg)







