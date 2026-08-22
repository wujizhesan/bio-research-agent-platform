import importlib.util
import unittest
from unittest.mock import AsyncMock, patch


@unittest.skipUnless(importlib.util.find_spec('chainlit'), 'Chainlit is optional')
class ChainlitAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_chainlit_runtime_helpers_exist_and_no_key_path_is_safe(self):
        from src import chainlit_app

        self.assertTrue(callable(chainlit_app._send_tool_result))
        self.assertTrue(callable(chainlit_app._llm_reply))
        with patch.object(chainlit_app.agent, 'load_llm_config', return_value=('http://localhost', 'test', None)):
            with patch.object(chainlit_app.cl, 'Message') as message:
                message.return_value.send = AsyncMock()
                await chainlit_app._llm_reply('test')
                message.assert_called_once()
                message.return_value.send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
