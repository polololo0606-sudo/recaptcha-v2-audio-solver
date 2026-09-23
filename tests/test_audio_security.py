import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'audio-solver' / 'RecaptchaSolver.py'
spec = importlib.util.spec_from_file_location('audio_solver', SOURCE)
solver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solver)
URL = 'https://www.google.com/recaptcha/api2/payload?p=opaque-test'
PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('142.250.74.196', 443))


class AudioSecurityTests(unittest.TestCase):
    def test_reject_untrusted_urls_before_network(self):
        bad = ['file:///tmp/test.mp3', 'http://www.google.com/recaptcha/api2/payload',
               'ftp://www.google.com/recaptcha/api2/payload', 'data:audio/mp3;base64,AA==',
               'https://127.0.0.1/recaptcha/api2/payload', 'https://2130706433/recaptcha/api2/payload',
               'https://[::1]/recaptcha/api2/payload', 'https://www.google.com.attacker.test/recaptcha/api2/payload',
               'https://attacker@www.google.com/recaptcha/api2/payload',
               'https://www.google.com:444/recaptcha/api2/payload',
               'https://www.google.com:bad/recaptcha/api2/payload',
               'https://www.google.com./recaptcha/api2/payload',
               'https://www%2egoogle.com/recaptcha/api2/payload',
               'https://www.google.com/url?redirect=http://localhost',
               URL+'#fragment', ' '+URL, URL+'\n', URL+'\\evil',
               'https://www.google.com/recaptcha/api2/%70ayload']
        with patch.object(solver.socket, 'getaddrinfo') as dns:
            for url in bad:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    solver._download_audio(url)
            dns.assert_not_called()

    def test_supported_endpoint_forms(self):
        for host in ('www.google.com', 'www.recaptcha.net'):
            for path in solver._AUDIO_PATHS:
                for port in ('', ':443'):
                    url=solver._trusted_url(f'https://{host}{port}{path}?p=a%2Fb&k=example', solver._AUDIO_PATHS)
                    self.assertEqual(url.query, 'p=a%2Fb&k=example')

    def test_reject_nonpublic_and_mixed_dns(self):
        for ip in ('127.0.0.1','10.0.0.1','169.254.169.254','0.0.0.0','224.0.0.1',
                   '192.0.2.1','::1','fe80::1','fc00::1','ff02::1','::ffff:127.0.0.1',
                   '2002:7f00:1::','2001:0:4136:e378:8000:63bf:3fff:fdd2'):
            unsafe=(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6,'',(ip,443))
            with self.subTest(ip=ip), patch.object(solver.socket,'getaddrinfo',return_value=[PUBLIC,unsafe]), patch.object(solver.socket,'socket') as connect:
                with self.assertRaises(ValueError):solver._download_audio(URL)
                connect.assert_not_called()

    def transport(self, status=200, data=b'fixture-audio', length=None):
        response=Mock(status=status)
        response.getheader.return_value=length
        response.read1.side_effect=[data,b'']
        connection=Mock();connection.getresponse.return_value=response
        raw=Mock();tls=Mock();context=Mock();context.wrap_socket.return_value=tls
        return response,connection,raw,tls,context

    def test_pins_validated_address_and_preserves_tls_identity(self):
        response,connection,raw,tls,context=self.transport()
        with patch.dict('os.environ',{'HTTPS_PROXY':'http://127.0.0.1:9'}), patch.object(solver.socket,'getaddrinfo',return_value=[PUBLIC]) as dns, patch.object(solver.socket,'socket',return_value=raw), patch.object(solver.ssl,'create_default_context',return_value=context), patch.object(solver.http.client,'HTTPSConnection',return_value=connection):
            self.assertEqual(solver._download_audio(URL), b'fixture-audio')
            dns.assert_called_once_with('www.google.com',443,type=socket.SOCK_STREAM)
            raw.connect.assert_called_once_with(PUBLIC[-1])
            context.wrap_socket.assert_called_once_with(raw,server_hostname='www.google.com')
            self.assertIs(connection.sock,tls)
            connection.request.assert_called_once_with('GET','/recaptcha/api2/payload?p=opaque-test',headers={'User-Agent':'Mozilla/5.0'})
            connection.close.assert_called_once()

    def test_redirects_and_oversized_content_rejected(self):
        for status,length,data in [(302,None,b''),(307,None,b''),(200,str(solver._MAX_AUDIO_BYTES+1),b''),(200,None,b'x'*9)]:
            response,connection,raw,tls,context=self.transport(status,data,length)
            with self.subTest(status=status,length=length), patch.object(solver,'_MAX_AUDIO_BYTES',8), patch.object(solver.socket,'getaddrinfo',return_value=[PUBLIC]) as dns, patch.object(solver.socket,'socket',return_value=raw), patch.object(solver.ssl,'create_default_context',return_value=context), patch.object(solver.http.client,'HTTPSConnection',return_value=connection):
                with self.assertRaises(ValueError):solver._download_audio(URL)
                dns.assert_called_once()
                connection.request.assert_called_once()
                connection.close.assert_called_once()

    def test_imitation_and_hidden_frames_rejected(self):
        obj=solver.RecaptchaSolver(Mock())
        def frame(url,visible=True):return SimpleNamespace(url=url,states=SimpleNamespace(is_displayed=visible))
        bad=frame('https://attacker.test/recaptcha/api2/bframe')
        hidden=frame('https://www.google.com/recaptcha/api2/bframe',False)
        good=frame('https://www.google.com/recaptcha/api2/bframe?k=fixture')
        obj.driver.eles.return_value=[bad,hidden,good]
        self.assertIs(obj._get_challenge_frame(),good)
        good.url='https://attacker.test/recaptcha/api2/bframe'
        self.assertIsNone(obj._get_challenge_frame())

    def test_real_subprocess_deadline_and_reaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=Path(tmp)/'worker.py';worker.write_text('import time\ntime.sleep(30)\n')
            real_popen=subprocess.Popen;children=[]
            def track(*a,**kw):
                child=real_popen(*a,**kw);children.append(child);return child
            start=time.monotonic()
            with patch.object(solver,'__file__',str(worker)),patch.object(solver,'_RECOGNITION_TIMEOUT',.15),patch.object(subprocess,'Popen',side_effect=track):
                with self.assertRaises(TimeoutError):solver._transcribe_with_timeout('unused.wav')
            self.assertLess(time.monotonic()-start,2)
            self.assertEqual(len(children),1)
            self.assertIsNotNone(children[0].poll())

    def test_transcription_result_and_failure(self):
        with patch.object(solver.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps('  Hello World  '))):
            self.assertEqual(solver._transcribe_with_timeout('test.wav'),'hello world')
        with patch.object(solver.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
            with self.assertRaises(RuntimeError):solver._transcribe_with_timeout('test.wav')

    def test_speech_error_types_preserved_without_provider_details(self):
        for error,expected in [('UnknownValueError',solver.sr.UnknownValueError),('RequestError',solver.sr.RequestError)]:
            with self.subTest(error=error),patch.object(solver.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=json.dumps({'error':error}))):
                with self.assertRaises(expected):solver._transcribe_with_timeout('test.wav')

    def test_rejected_audio_never_transcribed_and_temp_removed(self):
        with tempfile.TemporaryDirectory() as root:
            tmp=Path(root)/'audio';tmp.mkdir()
            with patch.object(solver.tempfile,'mkdtemp',return_value=str(tmp)),patch.object(solver,'_transcribe_with_timeout') as transcribe:
                with self.assertRaises(ValueError):solver.RecaptchaSolver(Mock())._download_and_transcribe('file:///tmp/private.mp3')
                transcribe.assert_not_called()
            self.assertFalse(tmp.exists())

    def test_timeout_cleans_audio_and_does_not_submit(self):
        obj=solver.RecaptchaSolver(Mock());frame=Mock()
        with tempfile.TemporaryDirectory() as root:
            tmp=Path(root)/'audio';tmp.mkdir()
            audio=Mock();audio.export.side_effect=lambda path,format: Path(path).write_bytes(b'wave-fixture')
            with patch.object(obj,'_get_challenge_frame',return_value=frame),patch.object(obj,'_check_for_errors'),patch.object(obj,'_get_audio_url',return_value=URL),patch.object(obj,'_type_answer') as submit,patch.object(solver.time,'sleep'),patch.object(solver.tempfile,'mkdtemp',return_value=str(tmp)),patch.object(solver,'_download_audio',return_value=b'mp3-fixture'),patch.object(solver.AudioSegment,'from_mp3',return_value=audio),patch.object(solver,'_transcribe_with_timeout',side_effect=TimeoutError('deadline')):
                with self.assertRaises(TimeoutError):obj._solve_audio_challenge()
                submit.assert_not_called()
            self.assertFalse(tmp.exists())


if __name__ == '__main__':unittest.main()
