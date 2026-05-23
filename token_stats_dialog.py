import csv
import os
from datetime import datetime, timedelta
from tkinter import Toplevel, ttk, StringVar, IntVar
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

# 配置中文字体
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


def format_number(num):
    """格式化数字，大于1000用K表示"""
    if num >= 1000000:
        return f"{num / 1000000:.2f}M"
    elif num >= 1000:
        return f"{num / 1000:.1f}K"
    return str(num)


def format_date(date_str):
    """格式化日期字符串为更简洁形式"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%m/%d")
    except ValueError:
        return date_str


class TokenStatsDialog:
    def __init__(self, parent, app):
        self.app = app
        self.result = None
        self.dialog = Toplevel(parent)
        self.dialog.title("Token 统计详情")
        self.dialog.geometry("900x780")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # 状态变量
        self.current_view = StringVar(value="overview")
        self.time_range = IntVar(value=0)  # 0=all, 30=30days, 7=7days

        self.build_ui()
        self.refresh_data()

        self.dialog.wait_window()

    def build_ui(self):
        frame = ttk.Frame(self.dialog, padding=14)
        frame.pack(fill="both", expand=True)

        # 顶部控制栏
        control_frame = ttk.Frame(frame)
        control_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        # 视图切换
        ttk.Radiobutton(control_frame, text="Overview", variable=self.current_view,
                        value="overview", command=self.refresh_data).pack(side="left", padx=(0, 20))
        ttk.Radiobutton(control_frame, text="Models", variable=self.current_view,
                        value="models", command=self.refresh_data).pack(side="left", padx=(0, 20))

        # 时间范围选择（固定7天或30天）
        ttk.Label(control_frame, text="时间范围：").pack(side="left", padx=(20, 10))
        ttk.Radiobutton(control_frame, text="7天", variable=self.time_range,
                        value=7, command=self.refresh_data).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(control_frame, text="30天", variable=self.time_range,
                        value=30, command=self.refresh_data).pack(side="left", padx=(0, 10))

        # 导出按钮
        ttk.Button(control_frame, text="导出CSV", command=self.export_csv).pack(side="right")

        # 主内容区域
        content_frame = ttk.Frame(frame)
        content_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")

        # 左侧统计面板
        self.stats_frame = ttk.Frame(content_frame, padding=10)
        self.stats_frame.pack(side="left", fill="y", padx=(0, 10))

        # 右侧图表区域
        self.chart_frame = ttk.Frame(content_frame)
        self.chart_frame.pack(side="left", fill="both", expand=True)

        # 设置matplotlib图表
        self.fig = Figure(figsize=(7, 5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # 设置grid权重
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

    def refresh_data(self):
        """刷新数据和图表"""
        # 清除旧内容
        for widget in self.stats_frame.winfo_children():
            widget.destroy()

        # 获取数据
        daily_stats = self.app.daily_stats.get("daily_stats", {})
        time_days = self.time_range.get()
        filtered_stats = self.filter_by_time_range(daily_stats, time_days)

        # 计算汇总数据
        total_stats = self.aggregate_stats(filtered_stats)
        model_stats = self.aggregate_by_model(filtered_stats)

        # 添加本次会话统计（从session_models获取）
        session_models = self.app.session_models
        session_total = {
            "input_tokens": self.app.session_tokens.get("input_tokens", 0),
            "cache_miss_input_tokens": self.app.session_tokens.get("cache_miss_input_tokens", 0),
            "cache_hit_input_tokens": self.app.session_tokens.get("cache_hit_input_tokens", 0),
            "output_tokens": self.app.session_tokens.get("output_tokens", 0),
            "request_count": sum(m.get("request_count", 0) for m in session_models.values()),
        }

        view = self.current_view.get()
        if view == "overview":
            self.build_overview_stats(session_total, total_stats, filtered_stats)
            self.draw_overview_charts(filtered_stats, total_stats)
        else:
            self.build_models_stats(session_models, model_stats)
            self.draw_models_charts(filtered_stats, model_stats)

    def filter_by_time_range(self, daily_stats, time_days):
        """按时间范围筛选数据，只返回有数据的日期"""
        # 确保最少7天，最多30天
        time_days = max(7, min(30, time_days))

        cutoff_date = datetime.now() - timedelta(days=time_days)
        cutoff_str = cutoff_date.strftime("%Y-%m-%d")

        filtered = {}
        for date_key, stats in daily_stats.items():
            if date_key >= cutoff_str and (stats.get("input_tokens", 0) > 0 or stats.get("output_tokens", 0) > 0):
                filtered[date_key] = stats
        return filtered

    def aggregate_stats(self, daily_stats):
        """聚合所有统计数据"""
        total = {
            "input_tokens": 0,
            "cache_miss_input_tokens": 0,
            "cache_hit_input_tokens": 0,
            "output_tokens": 0,
            "request_count": 0,
        }
        for date_stats in daily_stats.values():
            total["input_tokens"] += date_stats.get("input_tokens", 0)
            total["cache_miss_input_tokens"] += date_stats.get("cache_miss_input_tokens", 0)
            total["cache_hit_input_tokens"] += date_stats.get("cache_hit_input_tokens", 0)
            total["output_tokens"] += date_stats.get("output_tokens", 0)
            total["request_count"] += date_stats.get("request_count", 0)
        return total

    def aggregate_by_model(self, daily_stats):
        """按模型聚合统计"""
        model_totals = {}
        for date_stats in daily_stats.values():
            models = date_stats.get("models", {})
            for model_name, model_data in models.items():
                if model_name not in model_totals:
                    model_totals[model_name] = {
                        "input_tokens": 0,
                        "cache_miss_input_tokens": 0,
                        "cache_hit_input_tokens": 0,
                        "output_tokens": 0,
                        "request_count": 0,
                    }
                model_totals[model_name]["input_tokens"] += model_data.get("input_tokens", 0)
                model_totals[model_name]["cache_miss_input_tokens"] += model_data.get("cache_miss_input_tokens", 0)
                model_totals[model_name]["cache_hit_input_tokens"] += model_data.get("cache_hit_input_tokens", 0)
                model_totals[model_name]["output_tokens"] += model_data.get("output_tokens", 0)
                model_totals[model_name]["request_count"] += model_data.get("request_count", 0)
        return model_totals

    def build_overview_stats(self, session_total, total_stats, filtered_stats):
        """构建Overview统计面板"""
        # 本次会话
        ttk.Label(self.stats_frame, text="本次会话", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self.stats_frame, text=f"输入：{format_number(session_total['input_tokens'])}").pack(anchor="w")
        if session_total.get("cache_hit_input_tokens", 0) > 0:
            ttk.Label(self.stats_frame, text=f"  缓存命中：{format_number(session_total['cache_hit_input_tokens'])}").pack(anchor="w")
            ttk.Label(self.stats_frame, text=f"  缓存未命中：{format_number(session_total['cache_miss_input_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"输出：{format_number(session_total['output_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"请求数：{session_total['request_count']}").pack(anchor="w")

        ttk.Separator(self.stats_frame, orient="horizontal").pack(fill="x", pady=10)

        # 历史统计
        time_label = f"最近{self.time_range.get()}天"
        ttk.Label(self.stats_frame, text=f"历史统计 ({time_label})", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self.stats_frame, text=f"输入：{format_number(total_stats['input_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"  缓存命中：{format_number(total_stats['cache_hit_input_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"  缓存未命中：{format_number(total_stats['cache_miss_input_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"输出：{format_number(total_stats['output_tokens'])}").pack(anchor="w")
        ttk.Label(self.stats_frame, text=f"请求数：{total_stats['request_count']}").pack(anchor="w")

        # 使用天数
        days_count = len(filtered_stats)
        first_date = self.app.daily_stats.get("first_use_date")
        last_date = self.app.daily_stats.get("last_use_date")
        ttk.Label(self.stats_frame, text=f"统计天数：{days_count}").pack(anchor="w", pady=(5, 0))
        if first_date and last_date:
            ttk.Label(self.stats_frame, text=f"首次使用：{first_date}").pack(anchor="w")
            ttk.Label(self.stats_frame, text=f"最近使用：{last_date}").pack(anchor="w")

    def build_models_stats(self, session_models, model_stats):
        """构建Models统计面板"""
        # 本次会话分模型
        ttk.Label(self.stats_frame, text="本次会话", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 5))
        if session_models:
            for model_name, data in session_models.items():
                ttk.Label(self.stats_frame, text=f"{model_name}:").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  输入：{format_number(data['input_tokens'])}").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  输出：{format_number(data['output_tokens'])}").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  请求数：{data['request_count']}").pack(anchor="w")
        else:
            ttk.Label(self.stats_frame, text="暂无数据").pack(anchor="w")

        ttk.Separator(self.stats_frame, orient="horizontal").pack(fill="x", pady=10)

        # 历史分模型统计
        time_label = f"最近{self.time_range.get()}天"
        ttk.Label(self.stats_frame, text=f"历史统计 ({time_label})", font=("Arial", 11, "bold")).pack(anchor="w", pady=(0, 5))
        if model_stats:
            for model_name, data in sorted(model_stats.items(),
                                           key=lambda x: x[1].get("input_tokens", 0), reverse=True):
                ttk.Label(self.stats_frame, text=f"{model_name}:").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  输入：{format_number(data['input_tokens'])}").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  输出：{format_number(data['output_tokens'])}").pack(anchor="w")
                ttk.Label(self.stats_frame, text=f"  请求数：{data['request_count']}").pack(anchor="w")
        else:
            ttk.Label(self.stats_frame, text="暂无数据").pack(anchor="w")

    def draw_overview_charts(self, filtered_stats, total_stats):
        """绘制Overview图表"""
        self.fig.clear()

        # 获取有数据的日期列表
        dates = sorted(filtered_stats.keys())

        # Chart 1: 每日用量柱状图
        ax1 = self.fig.add_subplot(121)
        if dates:
            input_tokens = [filtered_stats[d].get("input_tokens", 0) for d in dates]
            output_tokens = [filtered_stats[d].get("output_tokens", 0) for d in dates]

            x_labels = [format_date(d) for d in dates]
            x_pos = range(len(dates))

            # 堆叠柱状图
            ax1.bar(x_pos, input_tokens, label="输入", color="#4CAF50", alpha=0.8)
            ax1.bar(x_pos, output_tokens, label="输出", color="#2196F3", alpha=0.8,
                    bottom=input_tokens)

            ax1.set_xlabel("日期")
            ax1.set_ylabel("Token")
            ax1.set_title("每日用量")
            ax1.set_xticks(x_pos)
            ax1.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
            ax1.legend(loc="upper right")

            # 格式化y轴
            ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format_number(int(x))))
        else:
            ax1.text(0.5, 0.5, "暂无数据", ha="center", va="center", fontsize=12)
            ax1.set_title("每日用量")

        # Chart 2: 每日缓存命中/未命中柱状图
        ax2 = self.fig.add_subplot(122)
        if dates:
            cache_hit_tokens = [filtered_stats[d].get("cache_hit_input_tokens", 0) for d in dates]
            cache_miss_tokens = [filtered_stats[d].get("cache_miss_input_tokens", 0) for d in dates]

            # 堆叠柱状图
            ax2.bar(x_pos, cache_hit_tokens, label="缓存命中", color="#4CAF50", alpha=0.8)
            ax2.bar(x_pos, cache_miss_tokens, label="缓存未命中", color="#FF5722", alpha=0.8,
                    bottom=cache_hit_tokens)

            ax2.set_xlabel("日期")
            ax2.set_ylabel("Token")
            ax2.set_title("每日缓存")
            ax2.set_xticks(x_pos)
            ax2.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
            ax2.legend(loc="upper right")

            # 格式化y轴
            ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format_number(int(x))))
        else:
            ax2.text(0.5, 0.5, "暂无数据", ha="center", va="center", fontsize=12)
            ax2.set_title("每日缓存")

        self.fig.tight_layout()
        self.canvas.draw()

    def draw_models_charts(self, filtered_stats, model_stats):
        """绘制Models图表，按天显示模型使用量"""
        self.fig.clear()

        dates = sorted(filtered_stats.keys())

        if not dates:
            ax1 = self.fig.add_subplot(121)
            ax1.text(0.5, 0.5, "暂无数据", ha="center", va="center", fontsize=12)
            ax1.set_title("每日模型输入")
            ax2 = self.fig.add_subplot(122)
            ax2.text(0.5, 0.5, "暂无数据", ha="center", va="center", fontsize=12)
            ax2.set_title("每日模型请求")
            self.fig.tight_layout()
            self.canvas.draw()
            return

        # 收集所有出现的模型
        all_models = set()
        for date_stats in filtered_stats.values():
            models = date_stats.get("models", {})
            all_models.update(models.keys())
        all_models = sorted(all_models)

        # Chart 1: 每日模型输入token柱状图（堆叠）
        ax1 = self.fig.add_subplot(121)
        x_pos = range(len(dates))
        x_labels = [format_date(d) for d in dates]

        # 为每个模型准备数据
        colors = ["#4CAF50", "#2196F3", "#FF5722", "#9C27B0", "#FF9800", "#795548", "#607D8B"]
        bottom = [0] * len(dates)
        for i, model in enumerate(all_models):
            model_input = [filtered_stats[d].get("models", {}).get(model, {}).get("input_tokens", 0) for d in dates]
            color = colors[i % len(colors)]
            ax1.bar(x_pos, model_input, label=model, color=color, alpha=0.8, bottom=bottom)
            bottom = [b + m for b, m in zip(bottom, model_input)]

        ax1.set_xlabel("日期")
        ax1.set_ylabel("输入Token")
        ax1.set_title("每日模型输入")
        ax1.set_xticks(x_pos)
        ax1.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
        ax1.legend(loc="upper right", fontsize=8)
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: format_number(int(x))))

        # Chart 2: 每日模型请求数柱状图（堆叠）
        ax2 = self.fig.add_subplot(122)
        bottom = [0] * len(dates)
        for i, model in enumerate(all_models):
            model_requests = [filtered_stats[d].get("models", {}).get(model, {}).get("request_count", 0) for d in dates]
            color = colors[i % len(colors)]
            ax2.bar(x_pos, model_requests, label=model, color=color, alpha=0.8, bottom=bottom)
            bottom = [b + m for b, m in zip(bottom, model_requests)]

        ax2.set_xlabel("日期")
        ax2.set_ylabel("请求数")
        ax2.set_title("每日模型请求")
        ax2.set_xticks(x_pos)
        ax2.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
        ax2.legend(loc="upper right", fontsize=8)

        self.fig.tight_layout()
        self.canvas.draw()

    def export_csv(self):
        """导出CSV文件"""
        daily_stats = self.app.daily_stats.get("daily_stats", {})
        time_days = self.time_range.get()
        filtered_stats = self.filter_by_time_range(daily_stats, time_days)

        if not filtered_stats:
            return

        # 选择保存路径
        from tkinter import filedialog
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"token_stats_{datetime.now().strftime('%Y%m%d')}.csv"
        )
        if not filename:
            return

        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # 写入表头
            writer.writerow(["日期", "输入Token", "缓存命中", "缓存未命中", "输出Token", "请求数"])

            # 写入数据
            for date_key in sorted(filtered_stats.keys()):
                stats = filtered_stats[date_key]
                writer.writerow([
                    date_key,
                    stats.get("input_tokens", 0),
                    stats.get("cache_hit_input_tokens", 0),
                    stats.get("cache_miss_input_tokens", 0),
                    stats.get("output_tokens", 0),
                    stats.get("request_count", 0),
                ])

                # 写入模型明细
                models = stats.get("models", {})
                for model_name, model_data in models.items():
                    writer.writerow([
                        f"  {model_name}",
                        model_data.get("input_tokens", 0),
                        model_data.get("cache_hit_input_tokens", 0),
                        model_data.get("cache_miss_input_tokens", 0),
                        model_data.get("output_tokens", 0),
                        model_data.get("request_count", 0),
                    ])

        # 显示成功提示
        from tkinter import messagebox
        messagebox.showinfo("导出成功", f"数据已导出到：{filename}")