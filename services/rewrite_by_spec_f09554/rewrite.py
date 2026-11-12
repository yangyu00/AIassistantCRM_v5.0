from services.product_info_raw import ProductInfoRaw


class ProductInfoRewrite(ProductInfoRaw):
    def __init__(self, product_id, custom_label_info, record=None):
        super().__init__(product_id, custom_label_info)
        self.record = record
        self.product_name = self.__get_product_name()
        self.output_script = self.__get_output_script()

    def __get_product_name(self):
        if self.product_id == "202608041615335173":
            return "号卡业务回捞商机"

        if self.product_id == "202608041615335172":
            return "套餐业务回捞商机"

        if self.product_id == "202608041615335174":
            return "宽带业务回捞商机"

    def __get_output_script(self):
        # 从独立数据库查询模块获取最近一条记录
        output_script = ''
        if self.record:
            output_script += '<strong>【通话总结】</strong>' + (self.record.get('content_abstract') or '无') + '\n'
            output_script += '<strong>【用户需求】</strong>' + f'<strong><span style="color:blue;">{self.record.get("type_xl")}：</span></strong>' + (
                    self.record.get('cust_demand') or '无') + '\n'
            output_script += '<strong>【已推荐政策】</strong>' + (self.record.get('rec_strategy') or '无') + '\n'
            output_script += '<strong>【拒绝原因】</strong>' + '\n' + '<strong><span style="color:blue;">拒绝原因：</span></strong>' + (
                    self.record.get(
                        'reject_reason') or '无'
            ) + '\n' + '<strong><span style="color:blue;">具体表述：</span></strong>' + (
                                     self.record.get('reason_note') or '无') + '\n'

        output_script += (self.auxiliary_portrait + '\n') if self.auxiliary_portrait else ""
        output_script = output_script.strip().replace('\n', '<br>')

        return output_script

    def __str__(self):
        return (f'Product ID: {self.product_id}\n'
                f'Product Name: {self.product_name}\n'
                f'queried_label_ids: {self.queried_label_ids}\n'
                f'queried_label_nvs: {self.queried_label_nvs}\n'
                f'auxiliary_portrait: {self.auxiliary_portrait}\n'
                f'output_script: {self.output_script}')
