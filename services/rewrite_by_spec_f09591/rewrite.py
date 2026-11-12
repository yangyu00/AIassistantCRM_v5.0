import re

from services.product_info_raw import ProductInfoRaw


class ProductInfoRewrite(ProductInfoRaw):
    def __init__(self, product_id, custom_label_info, record=None):
        super().__init__(product_id, custom_label_info)
        self.product_name = "控损业务专用"
        self.record = record
        self.output_script = self.__get_output_script()

    def __parse_custom_tags(self, text):
        """
        将自定义格式的文本转换为HTML格式
        支持: <$B>, <$I>, <$U>, <#RRGGBB>, <$B$I#RRGGBB> 等组合标签, 以及 </> 闭合标签
        """
        result = []
        i = 0
        tag_stack = []  # 存储已开启的标签名，用于逆序闭合

        while i < len(text):
            # 同时匹配三种开始标签: <$...>, <#RRGGBB>, 以及结束标签 </>
            start_dollar_match = re.search(r'<\$([^>]+)>', text[i:])
            start_color_match = re.search(r'<#([0-9A-Fa-f]{6})>', text[i:])
            end_match = re.search(r'</>', text[i:])

            # 计算各标签在原文中的绝对位置
            pos_dollar = start_dollar_match.start() + i if start_dollar_match else float('inf')
            pos_color = start_color_match.start() + i if start_color_match else float('inf')
            pos_end = end_match.start() + i if end_match else float('inf')

            # 找出下一个出现的标签位置
            next_pos = min(pos_dollar, pos_color, pos_end)

            # 情况1：后面没有标签了，追加剩余文本并退出
            if next_pos == float('inf'):
                result.append(text[i:])
                break

            # 追加标签前的普通文本
            result.append(text[i:next_pos])

            # 情况2：下一个是 <$...> 标签（如 <$B>, <$B$I#0070C0>）
            if next_pos == pos_dollar:
                tag_content = start_dollar_match.group(1)
                if 'B' in tag_content:
                    result.append("<b>")
                    tag_stack.append("b")
                if 'I' in tag_content:
                    result.append("<i>")
                    tag_stack.append("i")
                if 'U' in tag_content:
                    result.append("<u>")
                    tag_stack.append("u")
                color_match = re.search(r'#([0-9A-Fa-f]{6})', tag_content)
                if color_match:
                    color_code = color_match.group(1)
                    result.append(f'<span style="color: #{color_code}">')
                    tag_stack.append("span")
                i = next_pos + len(start_dollar_match.group(0))

            # 情况3：下一个是 <#RRGGBB> 颜色标签
            elif next_pos == pos_color:
                color_code = start_color_match.group(1)
                result.append(f'<span style="color: #{color_code}">')
                tag_stack.append("span")
                i = next_pos + len(start_color_match.group(0))

            # 情况4：下一个是 </> 结束标签
            else:
                # 逆序闭合栈中所有标签
                while tag_stack:
                    tag = tag_stack.pop()
                    result.append(f"</{tag}>" if tag != "span" else "</span>")
                i = next_pos + len(end_match.group(0))

        return "".join(result)

    def __get_output_script(self):
        """
        解析 self.record['strategy_name'] 并生成格式化的话术脚本。
        """
        # 1. 安全获取 content
        raw_content = self.record.get('strategy_name', '')
        if not raw_content:
            return "无历史话术记录"

        # 2. 调用解析函数，转换所有自定义标签
        html_content = self.__parse_custom_tags(raw_content)
        html_content = html_content.strip().replace('\n', '<br>')

        return html_content

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'output_script: {self.output_script}')
