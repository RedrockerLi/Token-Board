# AI API 用量可视化

本项目支持多 AI 平台 API 使用数据的可视化分析，提供每日用量图表、月度趋势统计以及按模型的费用分摊计算。

## 支持平台

- **DeepSeek** — 解析 amount-*.csv 和 cost-*.csv 文件

通过 adapter 模式，可轻松扩展新的 AI 平台。只需在 `app/adapters/` 下添加新的适配器即可。

## 数据目录结构

```
data/
└── <platform>/          # 每个子目录对应一个平台
    └── <year>/           # 按年组织（可选）
        ├── amount-YYYY-M.csv
        └── cost-YYYY-M.csv
```

## 快速启动

```bash
# 开发模式
python3 server.py --port 5000

# 一键启动（自动查找可用端口）
bash start.sh
```

然后访问 `http://localhost:<PORT>`。

## 项目结构

```
Token_Board/
├── app/
│   ├── ir.py              # 中间表示 (IR) 数据模型
│   ├── adapters/          # 平台适配器
│   │   └── deepseek.py
│   ├── data_loader.py     # 数据加载和扫描
│   ├── cost_allocator.py  # 按比例分摊费用
│   ├── routes.py          # Flask API 端点
│   └── config.py          # 配置
├── static/                # 前端静态资源
│   ├── js/
│   │   ├── api.js         # HTTP 通信层
│   │   ├── charts.js      # ECharts 渲染
│   │   └── dashboard.js   # 页面编排
│   └── css/
│       └── dashboard.css
├── templates/
│   └── index.html
├── data/                  # CSV 数据文件
├── server.py              # 服务器入口
└── start.sh               # 一键启动脚本
```

## API 端点

| 端点 | 说明 |
|------|------|
| `/api/summary` | 跨月份聚合统计 |
| `/api/monthly` | 按月聚合统计 |
| `/api/daily` | 指定月份的每日明细 |
| `/api/models` | 模型列表及所属平台 |
| `/api/token_types` | Token 类型分布 |
| `/api/api_key_names` | 用户列表 |
| `/api/refresh` | 重新扫描数据目录 |

支持查询参数：`api_key_name`、`model`、`platform`、`year`、`month`。
