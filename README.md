# coin11-tb
使用uiautomator2自动化完成淘宝双11的金币任务，淘金币任务，芭芭农场任务，闲鱼任务，支付宝打卡任务。

## 拼多多收藏商品规格比价

用 `.venv/bin/python -m pip install -r requirements.txt` 安装依赖。把要采集的商品放到收藏，用 ADB 连接已解锁且已登录拼多多的安卓设备。每次运行 `scan`，脚本会重新打开拼多多，经「个人中心 → 商品收藏」进入「我的收藏」，下拉刷新一次，等列表稳定后开始采集。大飞体育用品数码店归为自家，其他已确认名称的店铺归为竞品。

```bash
.venv/bin/python pdd_price.py scan --limit 2
.venv/bin/python pdd_price.py scan
.venv/bin/python pdd_price.py scan --output-dir ~/Downloads
```

终端会按时间打印启动导航、刷新、页面识别、店铺及商品定位、规格选择、券前价和 Excel 导出日志。`scan --limit 2` 采集前两家店铺的全部收藏商品。`scan` 导出店铺和规格明细。明细包含店铺名、商品名称、关键名称、券前价、状态和原因。

每次读到店铺标题，后续商品就归到该标题对应的店铺。翻页后暂时看不到店铺标题时，继续沿用上一家。看到「精选推荐」时结束采集。

已抢光的规格跳过，读不到明确券前价时保留原因并将价格留空。跨次同款配对和差价命令还需核实稳定商品标识。

`scan` 每次运行生成一份 Excel，包含「规格明细」和右侧的「同规格比价」。比价按品牌、型号和球速匹配。确认筒数相同后，差价按自家券前价减竞品券前价计算。筒数待核对时保留两边券前价，差价留空。文件用本地运行开始时间命名，格式为 `YYYY-MM-DD_HH-mm-ss.xlsx`，同秒重名时追加序号。默认保存到项目的 `downloads/`，可用 `--output-dir` 改目录。每次成功写入后，输出目录只保留最近三份拼多多比价 Excel。

## 淘宝双11一起组队升级赛车：组队口令：`49🔐WgBHTuzrkG3₴ https://m.tb.cn/h.jcy3MMs  CZ005 找好友组队，一起赢加抵金`

需要安装adb，自行百度教程。
使用教程请看抖音
```
3.53 04/14 X@z.TY bnD:/ 自动化完成淘宝任务教程  https://v.douyin.com/i5xNsWVx/ 复制此链接，打开Dou音搜索，直接观看视频！
```
或者快手
```
https://v.kuaishou.com/nGmUFX 自动化完成淘宝任务教程 该作品在快手被播放过1次，点击链接，打开【快手极速版】直接观看！
```

先执行`pip install -r requirements.txt`安装依赖，然后执行对应的python文件即可。

## 点赞历史

[![Star History Chart](https://star-history.dera.page/svg?repos=czl0325/coin11-tb&type=Date)](https://star-history.dera.page/#czl0325/coin11-tbt&Date)
<br><br>
