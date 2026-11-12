import re
from services.product_info_raw import ProductInfoRaw


class ProductInfoRewrite(ProductInfoRaw):
    def __init__(self, product_id, custom_label_info, record=None):
        super().__init__(product_id, custom_label_info)
        self.product_name = "诊断业务专用"
        self.record = record
        self.output_script = self.__get_output_script()

    def __get_output_script(self):
        """
        解析 self.record['strategy_script'] 并生成格式化的话术脚本。
        """
        # 1. 安全获取 content
        raw_content = self.record.get('strategy_script', '')
        if not raw_content:
            return "无话术记录"

        # 2. 转换为自定义格式

        # 1. 预处理：去除首尾空白，并统一按换行符拆分为列表
        lines = raw_content.strip().split('\n')
        html_content = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 2. 智能识别标题（支持中文全角冒号 "：" 和 英文半角冒号 ":"）
            # 只要一行里有冒号，就把冒号前面的当作【动态标题】，后面的当作内容
            parts = re.split(r'[：:]', line, maxsplit=1)

            # 如果拆分后长度大于1，说明这一行包含了 标题 + 内容
            if len(parts) > 1:
                title = parts[0].strip()
                content = parts[1].strip()

                # 确保提取出来的标题是有效的
                if title:
                    # 3. 核心样式逻辑
                    # 标题统一先设为蓝色加粗
                    formatted_title = f'<strong><span style="color:blue;">{title}：</span></strong>'

                    # 动态判断当前标题是否为“资费摘要”
                    if title == "资费摘要":
                        # 如果是，标题和内容都标红加粗
                        formatted_title = f'<strong><span style="color:red;">{title}：</span></strong>'
                        formatted_content = f'<strong><span style="color:red;">{content}</span></strong>'
                    else:
                        # 其他标签内容保持普通文本
                        formatted_content = content

                    # 4. 拼装并拼接
                    html_content += f"{formatted_title} {formatted_content}\n"

        html_content = html_content.strip().replace('\n', '<br>')
        return html_content

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'output_script: {self.output_script}')
