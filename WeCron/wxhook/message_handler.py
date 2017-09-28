#coding: utf-8
from __future__ import unicode_literals, absolute_import
import logging
import json

from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from wechatpy.replies import TextReply, TransferCustomerServiceReply, ImageReply
from wechatpy.exceptions import WeChatClientException

from common import wechat_client
from remind.models import Remind
from .todo_parser import parse, ParseError

logger = logging.getLogger(__name__)


class WechatMessage(object):

    def __init__(self, message):
        self.message = message
        self.user = get_user_model().objects.get_or_fetch(message.source)

    @property
    def json_msg(self):
        return json.dumps(self.message._data, ensure_ascii=False, indent=2)

    def text_reply(self, reply_str):
        return TextReply(
            content=reply_str[:800],  # WeChat can only accept 2048 bytes of char
            message=self.message,
        ).render()

    def handle(self):
        logger.info('Get a %s %s from %s', getattr(self.message, 'event', ''),
                    self.message.type, self.user.nickname)
        handler = getattr(self, 'handle_%s' % self.message.type.lower(), self.handle_unknown)
        return handler()

    def handle_event(self):
        handler = getattr(self, 'handle_%s_event' % self.message.event.lower(), self.handle_unknown_event)
        return handler()

    def handle_text(self, reminder=None):
        try:
            if not reminder:
                reminder = parse(self.message.content, uid=self.message.source)
                reminder.owner = self.user
                if hasattr(self.message, 'media_id'):
                    # This is a voice message
                    reminder.media_id = self.message.media_id
                reminder.save()
            reply_lines = [
                '/:ok将在%s提醒你%s' % (reminder.time_until(), reminder.event or ''),
                '\n备注: %s' % reminder.desc,
                '时间: %s' % reminder.local_time_string()
            ]
            if reminder.has_repeat():
                reply_lines.append('重复: %s' % reminder.get_repeat_text())
            # TODO: add \U0001F449 to the left of 修改
            reply_lines.append('\n<a href="%s">修改/分享</a>' % reminder.get_absolute_url(True))
            return self.text_reply('\n'.join(reply_lines))
        except ParseError as e:
            return self.text_reply(unicode(e))
        except WeChatClientException:  # TODO: refine it
            pass
        except Exception as e:  # Catch all kinds of wired errors
            logger.exception('Semantic parse error')
        return self.text_reply(
            '\U0001F648抱歉，我还只是一个比较初级的定时机器人，理解不了您刚才所说的话：\n\n“%s”\n\n'
            '或者您可以换个姿势告诉我该怎么定时，比如这样：\n\n' 
            '“两个星期后提醒我去复诊”。\n'
            '“周五晚上提醒我打电话给老妈”。\n'
            '“每月20号提醒我还信用卡[捂脸]”。' % self.message.content
        )

    def welcome_text(self):
        return (
            '亲爱的 %s，这是我刚注册的微信号，功能还在开发中，使用过程中如有不便请及时向我反馈哦。\n\n'
            '现在，直接输入文字或者语音就可以快速创建提醒啦！请点击下面的“使用方法”查看如何创建提醒。\n\n'
            'PS 这是一个开源项目，代码都在<a href="https://github.com/polyrabbit/WeCron">\U0001F449这里</a>，欢迎有开发技能的同学参与进来！'
            % self.user.get_full_name()
        )

    def handle_subscribe_event(self):
        self.user.subscribe = True
        self.user.save(update_fields=['subscribe'])
        return self.text_reply(self.welcome_text())

    def handle_subscribe_scan_event(self):
        if not self.user.subscribe:
            self.user.subscribe = True
            self.user.save(update_fields=['subscribe'])
            wechat_client.message.send_text(self.user.openid, self.welcome_text())
        if self.message.scene_id.isdigit():
            # legacy, when wechat doesn't support string as scene id
            subscribe_remind = Remind.objects.filter(
                id__gt='%s-0000-0000-0000-000000000000' % (hex(int(self.message.scene_id)).replace('0x', ''))
            ).order_by('id').first()
        else:
            subscribe_remind = Remind.objects.filter(id=self.message.scene_id).first()
        if subscribe_remind:
            if subscribe_remind.add_participant(self.user.openid):
                logger.info('User(%s) participants a remind(%s)', self.user.nickname, unicode(subscribe_remind))
            return self.handle_text(subscribe_remind)
        return self.text_reply('')

    handle_scan_event = handle_subscribe_scan_event

    def handle_unsubscribe_event(self):
        self.user.subscribe = False
        if not self.user.get_time_reminds().exists():
            self.user.delete()
        else:
            self.user.save(update_fields=['subscribe'])
        return self.text_reply("Bye")

    def handle_unknown(self):
        return self.text_reply(
            '/:jj如需设置提醒，只需用语音或文字告诉我就行了，比如这样：\n\n' 
            '“两个星期后提醒我去复诊”。\n'
            '“周五晚上提醒我打电话给老妈”。\n'
            '“每月20号提醒我还信用卡[捂脸]”。'
        )

    def handle_unknown_event(self):
        return self.handle_unknown()
        # return self.text_reply(
        #     'Hi %s! your %s event is\n%s' % (
        #         self.user.get_full_name(), self.message.event.lower(), self.json_msg)
        # )

    def handle_voice(self):
        self.message.content = getattr(self.message, 'recognition', '')
        if not self.message.content:
            return self.text_reply(
                '\U0001F648哎呀，看起来微信的语音转文字功能又双叒叕罬蝃抽风了，请重试一遍，或者直接发文字给我~'
            )
        return self.handle_text()

    def handle_location_event(self):
        return self.text_reply('\U0001F4AA基于地理位置的提醒正在开发中，敬请期待~\n' + self.json_msg)

    handle_location = handle_location_event

    def handle_click_event(self):
        if self.message.key.lower() == 'time_remind_today':
            now = timezone.now()
            time_reminds = self.user.get_time_reminds().filter(time__date=now).order_by('time').all()
            remind_text_list = self.format_remind_list(time_reminds)
            if remind_text_list:
                return self.text_reply('/:sunHi %s, 你今天的提醒有:\n\n%s' % (self.user.get_full_name(),
                                                                       '\n'.join(remind_text_list)))
            return self.text_reply('/:coffee今天没有提醒，休息一下吧！')
        elif self.message.key.lower() == 'time_remind_tomorrow':
            tomorrow = timezone.now()+timedelta(days=1)
            time_reminds = self.user.get_time_reminds().filter(time__date=tomorrow).order_by('time').all()
            remind_text_list = self.format_remind_list(time_reminds, True)
            if remind_text_list:
                return self.text_reply('/:sunHi %s, 你明天的提醒有:\n\n%s' % (self.user.get_full_name(),
                                                                       '\n'.join(remind_text_list)))
            return self.text_reply('/:coffee明天还没有提醒，休息一下吧！')
        elif self.message.key.lower() == 'customer_service':
            logger.info('Transfer to customer service')
            return TransferCustomerServiceReply(message=self.message).render()
        elif self.message.key.lower() == 'join_group':
            logger.info('Sending 小密圈 QR code')
            wechat_client.message.send_text(self.user.openid, u'喜欢微定时？请加入微定时小密圈，欢迎各种反馈和建议~')
            # http://mmbiz.qpic.cn/mmbiz_jpg/U4AEiaplkjQ3olQ6WLhRNIsLxb2LD4kdQSWN6PxulSiaY0dhwrY4HUVBBYFC8xawEd6Sf4ErGLk7EZTeD094ozxw/0?wx_fmt=jpeg
            return ImageReply(message=self.message, media_id='S8Jjk9aHXZ7wXSwK1qqu2UnkQSAHid-VQv_kxNUZnMI').render()
        elif self.message.key.lower() == 'donate':
            logger.info('Sending donation QR code')
            wechat_client.message.send_text(self.user.openid, u'好的服务离不开大家的鼓励和支持，如果觉得微定时给你的生活带来了一丝便利，'
                                                              u'请使劲用赞赏来支持(别忘了备注微信名，否则微信不让我看到是谁赞赏的)。')
            # http://mmbiz.qpic.cn/mmbiz_png/U4AEiaplkjQ26gI5kMFhaBda9CAcI5uxE4FDwWp8pOduoyBDDuWXtdgxx9UMH3GxUgrRoqibsqDHtwMMNjHJkjVg/0?wx_fmt=png
            return ImageReply(message=self.message, media_id='S8Jjk9aHXZ7wXSwK1qqu2b6yDboZT6UIvYWF4dKLyQs').render()
        elif self.message.key.lower() == 'add_friend':
            logger.info('Sending personal QR code')
            wechat_client.message.send_text(self.user.openid, u'长按下面的二维码，添加作者个人微信，等你来撩~')
            # http://mmbiz.qpic.cn/mmbiz_jpg/U4AEiaplkjQ1x2YoD9GRticXvMk5iaWJCtEVuChsHecnwdfHFbiafJarWXyiaABTu4pPUKibvnJ1ZGwUF7arzCaFkArw/0?wx_fmt=jpeg
            return ImageReply(message=self.message, media_id='S8Jjk9aHXZ7wXSwK1qqu2SXTItktLfgk4Cv9bod5l8k').render()
        return self.handle_unknown_event()

    @staticmethod
    def format_remind_list(reminds, next_run_found=False):
        now = timezone.now()
        remind_text_list = []
        for rem in reminds:
            emoji = '\U0001F552'  # Clock
            # takewhile is too aggressive
            if rem.time < now:
                emoji = '\U00002713 ' # Done
            elif not next_run_found:
                next_run_found = True
                emoji = '\U0001F51C' # Soon
            remind_text_list.append('%s %s - <a href="%s">%s</a>' %
                                    (emoji, rem.local_time_string('G:i'), rem.get_absolute_url(True), rem.title()))
        return remind_text_list


def handle_message(msg):
    # TODO unique based on msgid
    return WechatMessage(msg).handle()

