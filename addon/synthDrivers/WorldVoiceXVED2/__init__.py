﻿import os
import sys
base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
addon_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
synth_drivers_path = os.path.join(addon_path, 'synthDrivers', 'WorldVoiceXVED2')
sys.path.insert(0, base_dir)

from collections import OrderedDict
import config
from synthDriverHandler import SynthDriver, VoiceInfo, synthIndexReached, synthDoneSpeaking
import languageHandler
from logHandler import log
import speech

from . import _vocalizer
from ._voiceManager import VoiceManager
from . import languageDetection
from . import _config
from generics.models import SpeechSymbols
from . import speechcommand

import addonHandler
addonHandler.initTranslation()

import re
import driverHandler

number_pattern = re.compile(r"[0-9]+[0-9.:]*[0-9]+|[0-9]")
comma_number_pattern = re.compile(r"(?<=[0-9]),(?=[0-9])")
chinese_space_pattern = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")

class SynthDriver(SynthDriver):
	name = "WorldVoiceXVED2"
	description = "WorldVoice(VE)"
	supportedSettings = [
		SynthDriver.VoiceSetting(),
		# SynthDriver.VariantSetting(),
		SynthDriver.RateSetting(),
		SynthDriver.PitchSetting(),
		SynthDriver.VolumeSetting(),
		driverHandler.DriverSetting(
			"numlan",
			# Translators: Label for a setting in voice settings dialog.
			_("Number &Language"),
			availableInSettingsRing=True,
			defaultVal="default",
			# Translators: Label for a setting in synth settings ring.
			displayName=_("Number Language"),
		),
		driverHandler.DriverSetting(
			"nummod",
			# Translators: Label for a setting in voice settings dialog.
			_("Number &Mode"),
			availableInSettingsRing=True,
			defaultVal="value",
			# Translators: Label for a setting in synth settings ring.
			displayName=_("Number Mode"),
		),
		driverHandler.NumericDriverSetting(
			"chinesespace",
			# Translators: Label for a setting in voice settings dialog.
			_("Pause time when encountering spaces between Chinese"),
			defaultVal=0,
			minStep=1,
		),
		driverHandler.BooleanDriverSetting(
			"cni",
			_("Ignore comma between number"),
			defaultVal=False,
		),
		driverHandler.BooleanDriverSetting(
			"dli",
			_("Ignore language information of document"),
			defaultVal=False,
		),
		driverHandler.BooleanDriverSetting(
			"uwv",
			_("Enable WorldVoice setting rules to detect text language"),
			availableInSettingsRing=True,
			defaultVal=True,
			displayName=_("Enable WorldVoice rules"),
		),
	]
	supportedCommands = {
		speech.IndexCommand,
		speech.CharacterModeCommand,
		speech.LangChangeCommand,
		speech.BreakCommand,
		speech.PitchCommand,
		speech.RateCommand,
		speech.VolumeCommand,
	}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}

	@classmethod
	def check(cls):
		with _vocalizer.preOpenVocalizer() as check:
			return check

	def __init__(self):
		_config.load()
		# Initialize the driver
		try:
			_vocalizer.initialize(self._onIndexReached)
			log.debug("Vocalizer info: %s" % self._info())
		except _vocalizer.VeError as e:
			if e.code == _vocalizer.VAUTONVDA_ERROR_INVALID:
				log.info("Vocalizer license for NVDA is Invalid")
			elif e.code == _vocalizer.VAUTONVDA_ERROR_DEMO_EXPIRED:
				log.info("Vocalizer demo license for NVDA as expired.")
			raise
		self._voiceManager = VoiceManager()

		self._realSpeakFunc = speech.speak
		self._realSpellingFunc = speech.speakSpelling
		speech.speak = self.patchedSpeak
		speech.speakSpelling = self.patchedSpeakSpelling

		speechSymbols = SpeechSymbols()
		speechSymbols.load('unicode.dic')
		self._languageDetector = languageDetection.LanguageDetector(list(self._voiceManager.languages), speechSymbols)

		speech._speakWithoutPauses = speech.SpeechWithoutPauses(speakFunc=self.patchedSpeak)
		speech.speakWithoutPauses = speech._speakWithoutPauses.speakWithoutPauses

		self._localeToVoices = self._voiceManager.localeToVoicesMap
		self._locales = sorted([l for l in self._localeToVoices if len(self._localeToVoices[l]) > 0])
		self._localeNames = list(map(self._getLocaleReadableName, self._locales))

		self._voice = None

	def _onIndexReached(self, index):
		if index is not None:
			synthIndexReached.notify(synth=self, index=index)
		else:
			synthDoneSpeaking.notify(synth=self)

	def terminate(self):
		speech.speak = self._realSpeakFunc
		speech.speakSpelling = self._realSpellingFunc

		speech._speakWithoutPauses = speech.SpeechWithoutPauses(speakFunc=speech.speak)
		speech.speakWithoutPauses = speech._speakWithoutPauses.speakWithoutPauses

		try:
			self.cancel()
			self._voiceManager.close()
			_vocalizer.terminate()
		except RuntimeError:
			log.error("Vocalizer terminate", exc_info=True)

	def speak(self, speechSequence):
		if self.uwv \
			and _config.vocalizerConfig['autoLanguageSwitching']['useUnicodeLanguageDetection'] \
			and _config.vocalizerConfig['autoLanguageSwitching']['afterSymbolDetection']:
			speechSequence = self._languageDetector.add_detected_language_commands(speechSequence)
			speechSequence = list(speechSequence)
		speechSequence = self.patchedNumSpeechSequence(speechSequence)
		speechSequence = self.patchedSpaceSpeechSequence(speechSequence)

		currentInstance = defaultInstance = self._voiceManager.defaultVoiceInstance.token
		currentLanguage = defaultLanguage = self.language
		chunks = []
		hasText = False
		charMode = False
		for command in speechSequence:
			if isinstance(command, str):
				command = command.strip()
				if not command:
					continue
				# If character mode is on use lower case characters
				# Because the synth does not allow to turn off the caps reporting
				if charMode or len(command) == 1:
					command = command.lower()
				# replace the excape character since it is used for parameter changing
				chunks.append(command.replace("\x1b", ""))
				hasText = True
			elif isinstance(command, speech.IndexCommand):
				chunks.append("\x1b\\mrk=%d\\" % command.index)
			elif isinstance(command, speech.BreakCommand):
				maxTime = 6553 if self.variant == "bet2" else 65535
				breakTime = max(1, min(command.time, maxTime))
				self._speak(currentInstance, chunks)
				chunks = []
				hasText = False
				_vocalizer.processBreak(currentInstance, breakTime)
			elif isinstance(command, speech.CharacterModeCommand):
				charMode = command.state
				s = "\x1b\\tn=spell\\" if command.state else "\x1b\\tn=normal\\"
				chunks.append(s)
			elif isinstance(command, speech.LangChangeCommand) or isinstance(command, speechcommand.WVLangChangeCommand):
				if command.lang == currentLanguage:
					# Keep on the same voice.
					continue
				if command.lang is None:
					# No language, use default.
					currentInstance = defaultInstance
					currentLanguage = defaultLanguage
					continue
				# Changed language, lets see what we have.
				currentLanguage = command.lang
				newVoiceName = self._voiceManager.getVoiceNameForLanguage(currentLanguage)
				if newVoiceName is None:
					# No voice for this language, use default.
					newInstance = defaultInstance
				else:
					newInstance = self._voiceManager.getVoiceInstance(newVoiceName).token
				if newInstance == currentInstance:
					# Same voice, next command.
					continue
				if hasText: # We changed voice, send text we already have to vocalizer.
					self._speak(currentInstance, chunks)
					chunks = []
					hasText = False
				currentInstance = newInstance
			elif isinstance(command, speech.PitchCommand):
				pitch = self._voiceManager.getVoiceParameter(currentInstance, _vocalizer.VE_PARAM_PITCH, type_=int)
				pitchOffset = self._percentToParam(command.offset, _vocalizer.PITCH_MIN, _vocalizer.PITCH_MAX) - _vocalizer.PITCH_MIN
				chunks.append("\x1b\\pitch=%d\\" % (pitch+pitchOffset))
			elif isinstance(command, speechcommand.SplitCommand):
				self._speak(currentInstance, chunks)
				chunks = []
				hasText = False
		if chunks:
			self._speak(currentInstance, chunks)

	def _speak(self, voiceInstance, chunks):
		text = speech.CHUNK_SEPARATOR.join(chunks).replace("  \x1b", "\x1b")
		_vocalizer.processText2Speech(voiceInstance, text)

	def patchedSpeak(self, speechSequence, symbolLevel=None, priority=None):
		if self._cni:
			temp = []
			for command in speechSequence:
				if isinstance(command, str):
					temp.append(comma_number_pattern.sub(lambda m:'', command))
				else:
					temp.append(command)
			speechSequence = temp
		if self._dli:
			speechSequence = self.patchedRemoveLangChangeCommandSpeechSequence(speechSequence)
		if self.uwv \
			and _config.vocalizerConfig['autoLanguageSwitching']['useUnicodeLanguageDetection'] \
			and not _config.vocalizerConfig['autoLanguageSwitching']['afterSymbolDetection']:
			speechSequence = self._languageDetector.add_detected_language_commands(speechSequence)
			speechSequence = list(speechSequence)
		self._realSpeakFunc(speechSequence, symbolLevel, priority=priority)

	def patchedSpeakSpelling(self, text, locale=None, useCharacterDescriptions=False, priority=None):
		if config.conf["speech"]["autoLanguageSwitching"] \
			and _config.vocalizerConfig['autoLanguageSwitching']['useUnicodeLanguageDetection'] \
			and config.conf["speech"]["trustVoiceLanguage"]:
				for text, loc in self._languageDetector.process_for_spelling(text, locale):
					self._realSpellingFunc(text, loc, useCharacterDescriptions, priority=priority)
		else:
			self._realSpellingFunc(text, locale, useCharacterDescriptions, priority=priority)

	def cancel(self):
		_vocalizer.stop()

	def pause(self, switch):
		if switch:
			_vocalizer.pause()
		else:
			_vocalizer.resume()

	def _get_volume(self):
		return self._voiceManager.defaultVoiceInstance.volume

	def _set_volume(self, value):
		self._voiceManager.defaultVoiceInstance.volume = value
		self._voiceManager.defaultVoiceInstance.commit()

	def _get_rate(self):
		return self._voiceManager.defaultVoiceInstance.rate

	def _set_rate(self, value):
		self._voiceManager.defaultVoiceInstance.rate = value
		self._voiceManager.defaultVoiceInstance.commit()

	def _get_pitch(self):
		return self._voiceManager.defaultVoiceInstance.pitch

	def _set_pitch(self, value):
		self._voiceManager.defaultVoiceInstance.pitch = value
		self._voiceManager.defaultVoiceInstance.commit()

	def _getAvailableVoices(self):
		return self._voiceManager.voiceInfos

	def _get_voice(self):
		if self._voice is None:
			voice = self._voiceManager.getVoiceNameForLanguage(languageHandler.getLanguage())
			if voice is None:
				voice = list(self.availableVoices.keys())[0]
			return voice
		return self._voiceManager.defaultVoiceName

	def _set_voice(self, voiceName):
		self._voice = voiceName
		if voiceName == self._voiceManager.defaultVoiceName:
			return
		# Stop speech before setting a new voice to avoid voice instances
		# continuing speaking when changing voices for, e.g., say-all
		# See NVDA ticket #3540
		_vocalizer.stop()
		self._voiceManager.setDefaultVoice(voiceName)
		# Available variants are cached by default. As variants maybe different for each voice remove the cached value
		# if hasattr(self, '_availableVariants'):
			# del self._availableVariants
		# Synchronize with the synth so the parameters
		# we report are not from the previous voice.
		# _vocalizer.sync()

	def _get_variant(self):
		return self._voiceManager.defaultVoiceInstance.variant

	def _set_variant(self, name):
		self.cancel()
		self._voiceManager.defaultVoiceInstance.variant = name

	def _getAvailableVariants(self):
		dbs = self._voiceManager.defaultVoiceInstance.variants
		return OrderedDict([(d, VoiceInfo(d, d)) for d in dbs])

	def _get_availableLanguages(self):
		return self._voiceManager.languages

	def _get_language(self):
		return self._voiceManager.getVoiceLanguage()

	def _info(self):
		s = [self.description]
		return ", ".join(s)

	def _get_availableNumlans(self):
		return dict({
			"default": driverHandler.StringParameterInfo("default", _("default")),
		}, **{
			locale: driverHandler.StringParameterInfo(locale, name) for locale, name in zip(self._locales, self._localeNames)
		})

	def _get_numlan(self):
		return self._numlan

	def _set_numlan(self,value):
		self._numlan = value

	def _get_availableNummods(self):
		return dict({
			"value": driverHandler.StringParameterInfo("value", _("value")),
			"number": driverHandler.StringParameterInfo("number", _("number")),
		})

	def _get_nummod(self):
		return self._nummod

	def _set_nummod(self,value):
		self._nummod = value

	def _get_chinesespace(self):
		return self._chinesespace

	def _set_chinesespace(self,value):
		self._chinesespace = value

	def _get_cni(self):
		return self._cni

	def _set_cni(self,value):
		self._cni = value

	def _get_dli(self):
		return self._dli

	def _set_dli(self,value):
		self._dli = value

	def patchedNumSpeechSequence(self, speechSequence):
		return self.coercionNumberLangChange(speechSequence, self._numlan, self._nummod)

	def patchedSpaceSpeechSequence(self, speechSequence):
		if not int(self._chinesespace) == 0:
			joinString = ""
			tempSpeechSequence = []
			for command in speechSequence:
				if not isinstance(command, str):
					tempSpeechSequence.append(joinString)
					tempSpeechSequence.append(command)
					joinString = ""
				else:
					joinString += command
			tempSpeechSequence.append(joinString)
			speechSequence = tempSpeechSequence

			tempSpeechSequence = []
			for command in speechSequence:
				if isinstance(command, str):
					result = re.split(chinese_space_pattern, command)
					if len(result) == 1:
						tempSpeechSequence.append(command)
					else:
						temp = []
						for i in result:
							temp.append(i)
							temp.append(speech.BreakCommand(int(self._chinesespace) * 5))
						temp = temp[:-1]
						tempSpeechSequence += temp
				else:
					tempSpeechSequence.append(command)
			speechSequence = tempSpeechSequence
		return speechSequence

	def patchedRemoveLangChangeCommandSpeechSequence(self, speechSequence):
		result = []
		for command in speechSequence:
			if not isinstance(command, speech.LangChangeCommand):
				result.append(command)
		return result

	def patchedLengthSpeechSequence(self, speechSequence):
		result = []
		for command in speechSequence:
			if isinstance(command, str):
				result.extend(self.lengthsplit(command, 100))
			else:
				result.append(command)
		return result

	def lengthsplit(self, string, length):
		result = []
		pattern = re.compile(r"[\s]")
		spaces = pattern.findall(string)
		others = pattern.split(string)
		fragment = ""
		for other, space in zip(others, spaces):
			fragment += other + space
			if len(fragment) > length:
				result.append(fragment)
				result.append(speechcommand.SplitCommand())
				fragment = ""
		fragment += others[-1]
		result.append(fragment)
		return result

	def resplit(self, pattern, string, mode):
		result = []
		numbers = pattern.findall(string)
		others = pattern.split(string)
		for other, number in zip(others, numbers):
			if mode == 'value':
				result.extend([other, speech.LangChangeCommand('StartNumber'), number, speech.LangChangeCommand('EndNumber')])
			elif mode == 'number':
				result.extend([other, speech.LangChangeCommand('StartNumber'), ' '.join(number).replace(" . ", "."), speech.LangChangeCommand('EndNumber')])
		result.append(others[-1])
		return result

	def coercionNumberLangChange(self, speechSequence, numberLanguage, mode):
		result = []
		for command in speechSequence:
			if isinstance(command, str):
				result.extend(self.resplit(number_pattern, command, mode))
			else:
				result.append(command)

		currentLang = self.language
		for command in result:
			if isinstance(command, speech.LangChangeCommand):
				if command.lang == 'StartNumber':
					command.lang = numberLanguage
				elif command.lang == 'EndNumber':
					command.lang = currentLang
				else:
					currentLang = command.lang
		return result

	def _getLocaleReadableName(self, locale):
		description = languageHandler.getLanguageDescription(locale)
		return "%s" % (description) if description else locale
